from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import pytest

from quill.git_ops import GitError
from quill.project_board import ProjectBoard, derive_branch_name


def test_project_board_moves_matching_issue_to_named_status() -> None:
    commands: list[list[str]] = []

    def run(args: Sequence[str]) -> str:
        command = list(args)
        commands.append(command)
        if command[1:3] == ["project", "list"]:
            return json.dumps({"projects": [{"title": "Board", "number": 7, "id": "project"}]})
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(
                {"items": [{"id": "item", "content": {"number": 14, "repository": "me/repo"}}]}
            )
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(
                {
                    "fields": [
                        {
                            "id": "status-field",
                            "name": "Status",
                            "options": [{"id": "review", "name": "In review"}],
                        }
                    ]
                }
            )
        return ""

    ProjectBoard(run).move_issue("me/repo", 14, "Board", "In review")

    assert commands[-1] == [
        "gh",
        "project",
        "item-edit",
        "--id",
        "item",
        "--field-id",
        "status-field",
        "--project-id",
        "project",
        "--single-select-option-id",
        "review",
    ]


def _project_responses(
    *, items: list[dict[str, object]], hierarchy: list[dict[str, object]] | None = None
) -> tuple[dict[str, object], dict[str, object], dict[str, object], str]:
    projects: dict[str, object] = {
        "projects": [{"title": "Board", "number": 7, "id": "project"}],
        "totalCount": 1,
    }
    fields: dict[str, object] = {
        "fields": [
            {
                "id": "status-field",
                "name": "Status",
                "options": [
                    {"id": "backlog", "name": "Backlog"},
                    {"id": "queue", "name": "Queue"},
                    {"id": "progress", "name": "In progress"},
                ],
            }
        ],
        "totalCount": 1,
    }
    project_items: dict[str, object] = {"items": items, "totalCount": len(items)}
    page = {
        "data": {
            "repository": {
                "issues": {
                    "nodes": hierarchy or [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }
    return projects, fields, project_items, json.dumps([page])


def _issue(
    number: int,
    title: str,
    *,
    state: str = "OPEN",
    labels: tuple[str, ...] = (),
    parent: tuple[int, str] | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "labels": {"nodes": [{"name": label} for label in labels]},
        "parent": ({"number": parent[0], "title": parent[1], "state": "OPEN"} if parent else None),
    }


def _item(
    number: int,
    title: str,
    *,
    status: str = "Backlog",
    repo: str = "me/repo",
    content_type: str = "Issue",
) -> dict[str, object]:
    return {
        "id": f"item-{number}",
        "status": status,
        "content": {
            "number": number,
            "title": title,
            "repository": repo,
            "type": content_type,
        },
    }


def test_catalog_groups_native_children_and_standalone_in_numeric_order() -> None:
    items: list[dict[str, object]] = [
        _item(100, "Later child", status="Queue"),
        _item(4, "Epic"),
        _item(3, "First child"),
        _item(16, "Standalone"),
        _item(18, "Closed"),
        _item(19, "Other", repo="other/repo"),
        _item(20, "PR", content_type="PullRequest"),
        {"id": "draft", "content": None},
    ]
    hierarchy = [
        _issue(3, "First child", parent=(4, "Epic")),
        _issue(4, "Epic", labels=("EPIC",)),
        _issue(16, "Standalone"),
        _issue(18, "Closed", state="CLOSED"),
        _issue(100, "Later child", parent=(4, "Epic")),
    ]
    projects, fields, project_items, pages = _project_responses(items=items, hierarchy=hierarchy)

    def run(args: Sequence[str]) -> str:
        command = list(args)
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        if command[1:3] == ["api", "graphql"]:
            assert "--paginate" in command and "--slurp" in command
            return pages
        raise AssertionError(command)

    catalog = ProjectBoard(run).catalog("me/repo", "Board", ("epic",))

    assert [(group.epic_number, group.epic_title) for group in catalog.groups] == [
        (4, "Epic"),
        (None, "Standalone tickets"),
    ]
    assert [ticket.number for ticket in catalog.groups[0].tickets] == [3, 100]
    assert [ticket.number for ticket in catalog.groups[1].tickets] == [16]
    assert [ticket.number for ticket in catalog.tickets] == [3, 16, 100]
    assert catalog.groups[0].tickets[1].selectable is False
    assert [ticket.number for ticket in ProjectBoard(run).queue_items("me/repo", "Board")] == [100]


def test_repeated_queue_reads_refresh_items_without_reloading_unchanged_hierarchy() -> None:
    items = [_item(3, "Queued", status="Queue")]
    hierarchy = [_issue(3, "Queued")]
    projects, fields, project_items, pages = _project_responses(items=items, hierarchy=hierarchy)
    calls: list[tuple[str, str]] = []

    def run(args: Sequence[str]) -> str:
        command = list(args)
        calls.append((command[1], command[2]))
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        if command[1:3] == ["api", "graphql"]:
            return pages
        raise AssertionError(command)

    board = ProjectBoard(run)
    assert [item.number for item in board.queue_items("me/repo", "Board")] == [3]
    assert [item.number for item in board.queue_items("me/repo", "Board")] == [3]

    assert calls.count(("project", "item-list")) == 2
    assert calls.count(("api", "graphql")) == 1


def test_move_issues_is_idempotent_and_reports_partial_failures() -> None:
    commands: list[list[str]] = []
    items = [_item(3, "Already queued", status="Queue"), _item(16, "Move me")]
    projects, fields, project_items, _pages = _project_responses(items=items)

    def run(args: Sequence[str]) -> str:
        command = list(args)
        commands.append(command)
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        if command[1:3] == ["project", "item-edit"]:
            moved_id = command[command.index("--id") + 1]
            for item in items:
                if item["id"] == moved_id:
                    item["status"] = "Queue"
            return ""
        raise AssertionError(command)

    board = ProjectBoard(run)
    results = board.move_issues("me/repo", [100, 16, 3, 16], "Board", "Queue")

    assert [(row.ticket, row.success, row.changed) for row in results] == [
        (3, True, False),
        (16, True, True),
        (100, False, False),
    ]
    assert sum(command[1:3] == ["project", "item-edit"] for command in commands) == 1

    repeated = board.move_issues("me/repo", [16], "Board", "Queue")
    assert repeated[0].success and not repeated[0].changed
    assert sum(command[1:3] == ["project", "item-edit"] for command in commands) == 1


def test_move_issues_keeps_other_ticket_results_when_one_edit_fails() -> None:
    items = [_item(3, "Fails"), _item(16, "Works")]
    projects, fields, project_items, _pages = _project_responses(items=items)

    def run(args: Sequence[str]) -> str:
        command = list(args)
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        if command[1:3] == ["project", "item-edit"]:
            if command[command.index("--id") + 1] == "item-3":
                raise GitError("permission denied")
            return ""
        raise AssertionError(command)

    results = ProjectBoard(run).move_issues("me/repo", [3, 16], "Board", "Queue")

    assert [(row.ticket, row.success) for row in results] == [(3, False), (16, True)]
    assert results[0].error == "permission denied"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda projects, fields: fields.update(totalCount=2), "field list was incomplete"),
        (
            lambda projects, fields: (
                fields["fields"].append(fields["fields"][0].copy()),
                fields.update(totalCount=2),
            ),
            "no unique Status field",
        ),
        (
            lambda projects, fields: fields["fields"][0].update(
                options=[{"id": "backlog", "name": "Backlog"}]
            ),
            "no unique 'Queue' status",
        ),
    ],
)
def test_queue_capability_rejects_incomplete_or_ambiguous_metadata(
    mutate: Callable[[dict[str, object], dict[str, object]], None], message: str
) -> None:
    projects, fields, project_items, pages = _project_responses(items=[])
    assert callable(mutate)
    mutate(projects, fields)

    def run(args: Sequence[str]) -> str:
        command = list(args)
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        if command[1:3] == ["api", "graphql"]:
            return pages
        raise AssertionError(command)

    board = ProjectBoard(run)
    if "Queue" in message:
        with pytest.raises(GitError, match=message):
            board.catalog("me/repo", "Board")
    else:
        with pytest.raises(GitError, match=message):
            board.resolve("me", "Board")


def test_project_boundary_rejects_malformed_json_and_incomplete_items() -> None:
    with pytest.raises(GitError, match="malformed JSON"):
        ProjectBoard(lambda args: "not-json").resolve("me", "Board")

    projects, fields, project_items, _pages = _project_responses(items=[])
    project_items["totalCount"] = 1

    def run(args: Sequence[str]) -> str:
        command = list(args)
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        raise AssertionError(command)

    with pytest.raises(GitError, match="project item list was incomplete"):
        ProjectBoard(run).move_issues("me/repo", [3], "Board", "Queue")


def test_queue_items_refreshes_board_status_between_watcher_scans() -> None:
    item = _item(3, "Ticket")
    projects, fields, project_items, pages = _project_responses(
        items=[item], hierarchy=[_issue(3, "Ticket")]
    )

    def run(args: Sequence[str]) -> str:
        command = list(args)
        if command[1:3] == ["project", "list"]:
            return json.dumps(projects)
        if command[1:3] == ["project", "field-list"]:
            return json.dumps(fields)
        if command[1:3] == ["project", "item-list"]:
            return json.dumps(project_items)
        if command[1:3] == ["api", "graphql"]:
            return pages
        raise AssertionError(command)

    board = ProjectBoard(run)
    assert board.queue_items("me/repo", "Board") == ()
    item["status"] = "Queue"
    assert [row.number for row in board.queue_items("me/repo", "Board")] == [3]


@pytest.mark.parametrize(
    ("ticket", "title", "labels", "excluded", "expected"),
    [
        (
            127,
            "Implement Vllm Capabilities",
            ["documentation", "enhancement"],
            [],
            "enhancement/implement-vllm-capabilities_127",
        ),
        (
            126,
            "Fix Source Of Model Truth",
            ["enhancement", "bug"],
            [],
            "bug/fix-source-of-model-truth_126",
        ),
        (3, "Crème & API!", ["EPIC"], ["epic"], "feat/creme-api_3"),
        (4, "測試", [], [], "feat/ticket-4_4"),
        (5, "x" * 90, [], [], f"feat/{'x' * 80}_5"),
    ],
)
def test_derive_branch_name_matches_browser_rules(
    ticket: int,
    title: str,
    labels: list[str],
    excluded: list[str],
    expected: str,
) -> None:
    assert derive_branch_name(ticket, title, labels, excluded) == expected
