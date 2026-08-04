"""Read and update GitHub Project v2 issue status for Quill."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any, cast

from quill.git_ops import GitError, Runner


PROJECT_LIST_LIMIT = 1_000
PROJECT_ITEM_LIMIT = 10_000
PROJECT_FIELD_LIMIT = 1_000

_ISSUE_HIERARCHY_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $endCursor) {
      nodes {
        number
        title
        state
        labels(first: 100) { nodes { name } }
        parent { number title state }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()


@dataclass(frozen=True, slots=True)
class ProjectStatusOption:
    """One option in a Project v2 Status field."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ProjectMetadata:
    """Resolved identifiers required to read or mutate one Project v2 board."""

    owner: str
    title: str
    number: int
    id: str
    status_field_id: str
    status_options: tuple[ProjectStatusOption, ...]

    def status_option(self, name: str) -> ProjectStatusOption:
        """Return an exactly named Status option or raise an actionable error."""
        matches = [option for option in self.status_options if option.name == name]
        if len(matches) != 1:
            raise GitError(f"project '{self.title}' has no unique '{name}' status")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ProjectIssueItem:
    """One real repository Issue represented by a Project v2 item."""

    item_id: str
    repo: str
    number: int
    title: str
    labels: tuple[str, ...]
    status: str
    parent_number: int | None = None
    parent_title: str | None = None

    @property
    def selectable(self) -> bool:
        """Whether an operator may move this open issue into Queue."""
        return self.status.casefold() not in {
            "queue",
            "in progress",
            "in review",
            "done",
            "not doing",
        }


@dataclass(frozen=True, slots=True)
class ProjectIssueGroup:
    """Open child issues grouped under one native parent, or Standalone tickets."""

    epic_number: int | None
    epic_title: str
    tickets: tuple[ProjectIssueItem, ...]


@dataclass(frozen=True, slots=True)
class ProjectCatalog:
    """Queue-capable Project metadata and its grouped open issues."""

    project: ProjectMetadata
    groups: tuple[ProjectIssueGroup, ...]

    @property
    def tickets(self) -> tuple[ProjectIssueItem, ...]:
        """Return every visible ticket in numeric execution order."""
        return tuple(sorted((item for group in self.groups for item in group.tickets), key=_number))


@dataclass(frozen=True, slots=True)
class ProjectMoveResult:
    """Per-ticket result from an idempotent Project status batch mutation."""

    ticket: int
    success: bool
    changed: bool
    item_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _IssueMetadata:
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    parent_number: int | None
    parent_title: str | None


@dataclass(slots=True)
class ProjectBoard:
    """Bounded read/write access to GitHub Project v2 issue items."""

    run: Runner
    _projects: dict[tuple[str, str], ProjectMetadata] = field(default_factory=dict, init=False)
    _items: dict[tuple[str, str], tuple[dict[str, Any], ...]] = field(
        default_factory=dict, init=False
    )
    _hierarchies: dict[str, dict[int, _IssueMetadata]] = field(default_factory=dict, init=False)

    def resolve(self, owner: str, board_title: str) -> ProjectMetadata:
        """Resolve one named project and its unique Status field."""
        cache_key = (owner, board_title)
        if cached := self._projects.get(cache_key):
            return cached

        projects = self._object(
            self.run(
                [
                    "gh",
                    "project",
                    "list",
                    "--owner",
                    owner,
                    "--format",
                    "json",
                    "--limit",
                    str(PROJECT_LIST_LIMIT),
                ]
            )
        )
        # ``gh project list`` reports totalCount across open and closed projects even when
        # ``--closed`` is absent. Its rows are still fully paginated by ``--limit``.
        project_rows = self._list(projects, "projects")
        matches = [item for item in project_rows if item.get("title") == board_title]
        if len(matches) != 1:
            raise GitError(f"project board '{board_title}' was not found uniquely for {owner}")
        number, project_id = matches[0].get("number"), matches[0].get("id")
        if not isinstance(number, int) or not isinstance(project_id, str) or not project_id:
            raise GitError(f"project board '{board_title}' returned incomplete metadata")

        fields = self._object(
            self.run(
                [
                    "gh",
                    "project",
                    "field-list",
                    str(number),
                    "--owner",
                    owner,
                    "--format",
                    "json",
                    "--limit",
                    str(PROJECT_FIELD_LIMIT),
                ]
            )
        )
        field_rows = self._complete_list(fields, "fields", "project field list")
        status_fields = [row for row in field_rows if row.get("name") == "Status"]
        if len(status_fields) != 1 or not isinstance(status_fields[0].get("id"), str):
            raise GitError(f"project '{board_title}' has no unique Status field")
        raw_options = status_fields[0].get("options")
        if not isinstance(raw_options, list):
            raise GitError(f"project '{board_title}' Status field returned invalid options")
        options = tuple(
            ProjectStatusOption(id=option["id"], name=option["name"])
            for option in raw_options
            if isinstance(option, dict)
            and isinstance(option.get("id"), str)
            and option.get("id")
            and isinstance(option.get("name"), str)
            and option.get("name")
        )
        if len(options) != len(raw_options):
            raise GitError(f"project '{board_title}' Status field returned invalid options")
        duplicate_names = {
            option.name for option in options if sum(o.name == option.name for o in options) > 1
        }
        if duplicate_names:
            duplicate = sorted(duplicate_names)[0]
            raise GitError(f"project '{board_title}' has duplicate '{duplicate}' status options")

        metadata = ProjectMetadata(
            owner=owner,
            title=board_title,
            number=number,
            id=project_id,
            status_field_id=cast(str, status_fields[0]["id"]),
            status_options=options,
        )
        self._projects[cache_key] = metadata
        return metadata

    def is_queue_capable(self, owner: str, board_title: str) -> bool:
        """Return whether the board resolves with exactly one Queue Status option."""
        try:
            self.resolve(owner, board_title).status_option("Queue")
        except GitError:
            return False
        return True

    def catalog(
        self,
        repo: str,
        board_title: str,
        excluded_labels: tuple[str, ...] = (),
        *,
        refresh: bool = True,
        refresh_hierarchy: bool = True,
    ) -> ProjectCatalog:
        """Return open Project issues grouped by their native GitHub parent issue."""
        owner, _ = self._split_repo(repo)
        project = self.resolve(owner, board_title)
        project.status_option("Queue")
        raw_items = self._project_items(project, refresh=refresh)
        hierarchy = self._issue_hierarchy(repo, refresh=refresh_hierarchy)
        project_issue_numbers = {
            content["number"]
            for row in raw_items
            if isinstance((content := row.get("content")), dict)
            and content.get("type") == "Issue"
            and content.get("repository") == repo
            and isinstance(content.get("number"), int)
        }
        if not project_issue_numbers.issubset(hierarchy):
            # A newly added Project item can appear between hierarchy refreshes. Refresh once so
            # the watcher remains correct without paying for the full repository query every five
            # seconds while the board is unchanged.
            hierarchy = self._issue_hierarchy(repo, refresh=True)
        parent_numbers = {
            issue.parent_number for issue in hierarchy.values() if issue.parent_number is not None
        }
        excluded = {label.casefold() for label in excluded_labels}
        visible: list[ProjectIssueItem] = []
        for row in raw_items:
            content = row.get("content")
            if not isinstance(content, dict) or content.get("type") != "Issue":
                continue
            if content.get("repository") != repo:
                continue
            number = content.get("number")
            item_id = row.get("id")
            if not isinstance(number, int) or not isinstance(item_id, str) or not item_id:
                raise GitError(f"project '{board_title}' returned an invalid Issue item")
            issue = hierarchy.get(number)
            if issue is None:
                raise GitError(f"GitHub issue hierarchy omitted {repo}#{number}")
            if (
                issue.state != "OPEN"
                or number in parent_numbers
                or excluded.intersection(label.casefold() for label in issue.labels)
            ):
                continue
            status = row.get("status")
            visible.append(
                ProjectIssueItem(
                    item_id=item_id,
                    repo=repo,
                    number=number,
                    title=issue.title,
                    labels=issue.labels,
                    status=status if isinstance(status, str) else "",
                    parent_number=issue.parent_number,
                    parent_title=issue.parent_title,
                )
            )

        grouped: dict[tuple[int, str], list[ProjectIssueItem]] = {}
        standalone: list[ProjectIssueItem] = []
        for item in sorted(visible, key=_number):
            if item.parent_number is None:
                standalone.append(item)
            else:
                grouped.setdefault(
                    (item.parent_number, item.parent_title or f"Issue #{item.parent_number}"), []
                ).append(item)
        groups = [
            ProjectIssueGroup(number, title, tuple(tickets))
            for (number, title), tickets in sorted(grouped.items(), key=lambda pair: pair[0][0])
            if tickets
        ]
        if standalone:
            groups.append(ProjectIssueGroup(None, "Standalone tickets", tuple(standalone)))
        return ProjectCatalog(project=project, groups=tuple(groups))

    def queue_items(
        self,
        repo: str,
        board_title: str,
        excluded_labels: tuple[str, ...] = (),
        *,
        refresh: bool = True,
    ) -> tuple[ProjectIssueItem, ...]:
        """Return currently queued open issues in numeric execution order."""
        return tuple(
            item
            for item in self.catalog(
                repo,
                board_title,
                excluded_labels,
                refresh=refresh,
                refresh_hierarchy=False,
            ).tickets
            if item.status == "Queue"
        )

    def move_issues(
        self,
        repo: str,
        tickets: tuple[int, ...] | list[int],
        board_title: str,
        status: str,
    ) -> tuple[ProjectMoveResult, ...]:
        """Idempotently move unique tickets, reporting each mutation independently."""
        owner, _ = self._split_repo(repo)
        project = self.resolve(owner, board_title)
        option = project.status_option(status)
        rows = self._project_items(project, refresh=True)
        by_ticket: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            content = row.get("content")
            if (
                isinstance(content, dict)
                and content.get("type", "Issue") == "Issue"
                and content.get("repository") == repo
                and isinstance(content.get("number"), int)
            ):
                by_ticket.setdefault(cast(int, content["number"]), []).append(row)

        results: list[ProjectMoveResult] = []
        for ticket in sorted(set(tickets)):
            matches = by_ticket.get(ticket, [])
            item_id = matches[0].get("id") if len(matches) == 1 else None
            if len(matches) != 1 or not isinstance(item_id, str) or not item_id:
                results.append(
                    ProjectMoveResult(
                        ticket=ticket,
                        success=False,
                        changed=False,
                        error=(
                            f"issue {repo}#{ticket} is not a unique item on project '{board_title}'"
                        ),
                    )
                )
                continue
            if matches[0].get("status") == status:
                results.append(
                    ProjectMoveResult(ticket=ticket, success=True, changed=False, item_id=item_id)
                )
                continue
            try:
                self.run(
                    [
                        "gh",
                        "project",
                        "item-edit",
                        "--id",
                        item_id,
                        "--field-id",
                        project.status_field_id,
                        "--project-id",
                        project.id,
                        "--single-select-option-id",
                        option.id,
                    ]
                )
            except GitError as exc:
                results.append(
                    ProjectMoveResult(
                        ticket=ticket,
                        success=False,
                        changed=False,
                        item_id=item_id,
                        error=str(exc),
                    )
                )
            else:
                matches[0]["status"] = status
                results.append(
                    ProjectMoveResult(ticket=ticket, success=True, changed=True, item_id=item_id)
                )
        return tuple(results)

    def move_issue(self, repo: str, ticket: int, board_title: str, status: str) -> None:
        """Compatibility entry point for one deterministic run-status transition."""
        result = self.move_issues(repo, [ticket], board_title, status)[0]
        if not result.success:
            raise GitError(result.error or f"could not move issue {repo}#{ticket}")

    def invalidate(self, owner: str | None = None, board_title: str | None = None) -> None:
        """Clear cached reads globally or for one named board."""
        if owner is None and board_title is None:
            self._projects.clear()
            self._items.clear()
            self._hierarchies.clear()
            return
        if owner is None or board_title is None:
            raise ValueError("owner and board_title must be provided together")
        cache_key = (owner, board_title)
        self._projects.pop(cache_key, None)
        self._items.pop(cache_key, None)

    def invalidate_hierarchy(self, repo: str | None = None) -> None:
        """Clear cached issue hierarchy globally or for one repository."""
        if repo is None:
            self._hierarchies.clear()
        else:
            self._hierarchies.pop(repo, None)

    def _project_items(
        self, project: ProjectMetadata, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        cache_key = (project.owner, project.title)
        if not refresh and cache_key in self._items:
            return list(self._items[cache_key])
        payload = self._object(
            self.run(
                [
                    "gh",
                    "project",
                    "item-list",
                    str(project.number),
                    "--owner",
                    project.owner,
                    "--format",
                    "json",
                    "--limit",
                    str(PROJECT_ITEM_LIMIT),
                ]
            )
        )
        rows = self._complete_list(payload, "items", "project item list")
        self._items[cache_key] = tuple(dict(item) for item in rows)
        return list(self._items[cache_key])

    def _issue_hierarchy(self, repo: str, *, refresh: bool = False) -> dict[int, _IssueMetadata]:
        if not refresh and repo in self._hierarchies:
            return self._hierarchies[repo]
        owner, name = self._split_repo(repo)
        raw = self.run(
            [
                "gh",
                "api",
                "graphql",
                "--paginate",
                "--slurp",
                "-f",
                f"query={_ISSUE_HIERARCHY_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
            ]
        )
        pages = self._json(raw)
        page_rows = pages if isinstance(pages, list) else [pages]
        issues: dict[int, _IssueMetadata] = {}
        for page in page_rows:
            if not isinstance(page, dict):
                raise GitError("GitHub issue hierarchy returned an unexpected JSON shape")
            data = page.get("data")
            repository = data.get("repository") if isinstance(data, dict) else None
            issue_connection = repository.get("issues") if isinstance(repository, dict) else None
            nodes = issue_connection.get("nodes") if isinstance(issue_connection, dict) else None
            if not isinstance(nodes, list):
                raise GitError("GitHub issue hierarchy returned an unexpected JSON shape")
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                number, title, state = node.get("number"), node.get("title"), node.get("state")
                if (
                    not isinstance(number, int)
                    or not isinstance(title, str)
                    or not isinstance(state, str)
                ):
                    raise GitError("GitHub issue hierarchy returned invalid issue metadata")
                labels_value = node.get("labels")
                label_nodes = labels_value.get("nodes") if isinstance(labels_value, dict) else []
                label_nodes = label_nodes if isinstance(label_nodes, list) else []
                label_names: list[str] = []
                for label in label_nodes:
                    if not isinstance(label, dict):
                        continue
                    label_name = label.get("name")
                    if isinstance(label_name, str):
                        label_names.append(label_name)
                labels = tuple(label_names)
                parent = node.get("parent")
                parent_number = parent.get("number") if isinstance(parent, dict) else None
                parent_title = parent.get("title") if isinstance(parent, dict) else None
                issues[number] = _IssueMetadata(
                    number=number,
                    title=title,
                    state=state,
                    labels=labels,
                    parent_number=parent_number if isinstance(parent_number, int) else None,
                    parent_title=parent_title if isinstance(parent_title, str) else None,
                )
        self._hierarchies[repo] = issues
        return issues

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise GitError(f"invalid GitHub repository name '{repo}'")
        return parts[0], parts[1]

    @staticmethod
    def _json(raw: str) -> object:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise GitError("GitHub project command returned malformed JSON") from exc

    @classmethod
    def _object(cls, raw: str) -> dict[str, Any]:
        value = cls._json(raw)
        if not isinstance(value, dict):
            raise GitError("GitHub project command returned an unexpected JSON shape")
        return cast(dict[str, Any], value)

    @classmethod
    def _complete_list(
        cls, value: dict[str, Any], key: str, description: str
    ) -> list[dict[str, Any]]:
        rows = cls._list(value, key)
        total = value.get("totalCount")
        if isinstance(total, int) and total != len(rows):
            raise GitError(
                f"GitHub {description} was incomplete: received {len(rows)} of {total} records"
            )
        return rows

    @staticmethod
    def _list(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
        items = value.get(key)
        if not isinstance(items, list):
            return []
        return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def _number(item: ProjectIssueItem) -> int:
    return item.number


_WORK_TYPE_PRIORITY = (
    "bug",
    "fix",
    "enhancement",
    "feature",
    "feat",
    "refactor",
    "chore",
    "documentation",
    "docs",
    "ci",
    "test",
)


def derive_branch_name(
    ticket: int,
    title: str,
    labels: tuple[str, ...] | list[str],
    excluded_labels: tuple[str, ...] | list[str] = (),
) -> str:
    """Derive the create-run branch used by the browser for an issue ticket."""
    excluded = {label.casefold() for label in excluded_labels}
    available = [label.casefold() for label in labels if label.casefold() not in excluded]
    work_type = next(
        (candidate for candidate in _WORK_TYPE_PRIORITY if candidate in available),
        available[0] if available else "feat",
    )
    prefix = _branch_slug(work_type) or "feat"
    slug = _branch_slug(title)[:80].rstrip("-")
    return f"{prefix}/{slug or f'ticket-{ticket}'}_{ticket}"


def _branch_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    output: list[str] = []
    separator = False
    for character in ascii_value.lower():
        if "a" <= character <= "z" or "0" <= character <= "9":
            output.append(character)
            separator = False
        elif output and not separator:
            output.append("-")
            separator = True
    return "".join(output).strip("-")
