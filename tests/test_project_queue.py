from __future__ import annotations

import threading
from pathlib import Path

import pytest

from quill.project_board import (
    ProjectCatalog,
    ProjectIssueGroup,
    ProjectIssueItem,
    ProjectMetadata,
    ProjectMoveResult,
    ProjectStatusOption,
)
from quill_api.db import History, ProjectQueueItem, ProjectQueueItemSpec
from quill_api.project_queue import ProjectQueueCoordinator, PullRequestState
from quill_api.repository_registry import ConfiguredRepository, ConfiguredRepositoryRegistry
from quill_api.state import RunState, RunStatus, RunStore


REPOSITORY = ConfiguredRepository(
    name="me/game",
    visibility="PRIVATE",
    updated_at="now",
    default_branch="main",
    config_sha="abc",
    project_board="Game",
    excluded_issue_labels=("epic",),
)


class FakeBoard:
    def __init__(self, items: list[ProjectIssueItem]) -> None:
        self.items = items
        self.fail: set[int] = set()
        self.done: list[int] = []

    def catalog(
        self, repo: str, board_title: str, excluded_labels: tuple[str, ...] = ()
    ) -> ProjectCatalog:
        metadata = ProjectMetadata(
            owner="me",
            title="Game",
            number=1,
            id="project",
            status_field_id="status",
            status_options=(
                ProjectStatusOption("backlog", "Backlog"),
                ProjectStatusOption("queue", "Queue"),
                ProjectStatusOption("done", "Done"),
            ),
        )
        return ProjectCatalog(
            metadata,
            (
                ProjectIssueGroup(
                    1,
                    "Foundation",
                    tuple(sorted(self.items, key=lambda item: item.number)),
                ),
            ),
        )

    def queue_items(
        self, repo: str, board_title: str, excluded_labels: tuple[str, ...] = ()
    ) -> tuple[ProjectIssueItem, ...]:
        return tuple(item for item in self.items if item.status == "Queue")

    def move_issues(
        self, repo: str, tickets: tuple[int, ...] | list[int], board_title: str, status: str
    ) -> tuple[ProjectMoveResult, ...]:
        results = []
        for ticket in sorted(tickets):
            item = next(item for item in self.items if item.number == ticket)
            if ticket in self.fail:
                results.append(ProjectMoveResult(ticket, False, False, error="denied"))
                continue
            self.items[self.items.index(item)] = ProjectIssueItem(
                item.item_id,
                item.repo,
                item.number,
                item.title,
                item.labels,
                status,
                item.parent_number,
                item.parent_title,
            )
            results.append(ProjectMoveResult(ticket, True, True, item.item_id))
        return tuple(results)

    def move_issue(self, repo: str, ticket: int, board_title: str, status: str) -> None:
        assert status == "Done"
        self.done.append(ticket)

    def issue_titles(self, repo: str, *, refresh: bool = False) -> dict[int, str]:
        return {item.number: item.title for item in self.items}


def issue(number: int, status: str = "Backlog") -> ProjectIssueItem:
    return ProjectIssueItem(
        f"item-{number}",
        "me/game",
        number,
        f"Ticket {number}",
        ("enhancement",),
        status,
        1,
        "Foundation",
    )


@pytest.fixture
def setup(
    tmp_path: Path,
) -> tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]]:
    history = History("sqlite+pysqlite:///:memory:")
    registry = ConfiguredRepositoryRegistry(tmp_path / "repositories.json")
    registry._repositories = (REPOSITORY,)
    board = FakeBoard([issue(3), issue(16)])
    admitted: list[RunState] = []
    now = [100.0]

    def admit(item: ProjectQueueItem) -> RunState:
        ticket = item.ticket
        state = RunState(
            run_id=f"run-{ticket}",
            repo="me/game",
            branch=item.branch,
            ticket=ticket,
        )
        admitted.append(state)
        return state

    coordinator = ProjectQueueCoordinator(
        history,
        registry,
        RunStore(),
        board_factory=lambda: board,
        admit_root=admit,
        idle=lambda: True,
        publish=lambda: None,
        admission_lock=threading.Lock(),
        interval_s=5,
        enabled=False,
        clock=lambda: now[0],
    )
    return coordinator, history, board, admitted, now


def test_partial_batch_retains_only_successful_project_mutations(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, history, board, _admitted, _now = setup
    board.fail.add(16)

    result = coordinator.add_batch(REPOSITORY, [16, 3])

    assert result.batch_id is not None
    assert [(item.ticket, item.queued) for item in result.results] == [(3, True), (16, False)]
    assert [item.ticket for item in history.list_project_queue_items()] == [3]


def test_remove_items_returns_unstarted_tickets_to_their_prior_status(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, history, board, _admitted, _now = setup
    coordinator.add_batch(REPOSITORY, [3, 16])

    result = coordinator.remove_items(REPOSITORY, [3])

    assert [(item.ticket, item.removed, item.reason) for item in result.results] == [
        (3, True, None)
    ]
    assert next(item for item in board.items if item.number == 3).status == "Backlog"
    assert history.find_active_project_queue_item("me/game", 3) is None
    assert history.find_active_project_queue_item("me/game", 16) is not None


def test_remove_items_rejects_work_that_has_started(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, history, board, _admitted, now = setup
    coordinator.add_batch(REPOSITORY, [3])
    coordinator.scan_once()
    now[0] += 5
    coordinator.scan_once()

    result = coordinator.remove_items(REPOSITORY, [3])

    assert result.results[0].removed is False
    assert result.results[0].reason == "ticket has already started (running)"
    assert next(item for item in board.items if item.number == 3).status == "Queue"
    assert history.find_active_project_queue_item("me/game", 3) is not None


def test_changed_snapshot_waits_five_seconds_then_admits_only_numeric_head(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, history, _board, admitted, now = setup
    coordinator.add_batch(REPOSITORY, [16, 3])

    coordinator.scan_once()
    assert admitted == []
    now[0] += 4.9
    coordinator.scan_once()
    assert admitted == []
    now[0] += 0.1
    coordinator.scan_once()

    assert [run.ticket for run in admitted] == [3]
    pending = history.find_active_project_queue_item("me/game", 16)
    assert pending is not None and pending.state == "pending"


def test_snapshot_change_restarts_stabilization_window(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, _history, board, admitted, now = setup
    coordinator.add_batch(REPOSITORY, [3])
    coordinator.scan_once()
    now[0] += 4
    board.items.append(issue(16, "Queue"))
    coordinator.scan_once()
    now[0] += 4
    coordinator.scan_once()
    assert admitted == []
    now[0] += 1
    coordinator.scan_once()
    assert [run.ticket for run in admitted] == [3]


def test_failed_root_pauses_head_and_never_admits_later_ticket(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, history, _board, admitted, now = setup
    coordinator.add_batch(REPOSITORY, [3, 16])
    coordinator.scan_once()
    now[0] += 5
    coordinator.scan_once()
    failed = admitted[0]
    failed.status = RunStatus.FAILED
    failed.error = "tests failed"

    coordinator.on_run_terminal(failed)
    coordinator.scan_once()

    item = history.find_active_project_queue_item("me/game", 3)
    assert item is not None and item.state == "paused" and item.error == "tests failed"
    assert [run.ticket for run in admitted] == [3]


def test_exact_merged_pr_completes_head_and_releases_next(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, history, board, admitted, now = setup
    coordinator.add_batch(REPOSITORY, [3, 16])
    coordinator.scan_once()
    now[0] += 5
    coordinator.scan_once()
    root = admitted[0]
    root.status = RunStatus.DONE
    root.pr_number = 21
    monkeypatch.setattr(
        "quill_api.project_queue.read_pull_request",
        lambda _repo, _number: PullRequestState(21, "MERGED", root.branch or "", frozenset({3})),
    )

    coordinator.on_run_terminal(root)

    assert history.find_active_project_queue_item("me/game", 3) is None
    assert board.done == [3]
    assert [run.ticket for run in admitted] == [3, 16]


def test_closed_unmerged_pr_pauses_without_advancing(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, history, _board, admitted, now = setup
    coordinator.add_batch(REPOSITORY, [3, 16])
    coordinator.scan_once()
    now[0] += 5
    coordinator.scan_once()
    root = admitted[0]
    root.status = RunStatus.DONE
    root.pr_number = 21
    monkeypatch.setattr(
        "quill_api.project_queue.read_pull_request",
        lambda _repo, _number: PullRequestState(21, "CLOSED", root.branch or "", frozenset({3})),
    )

    coordinator.on_run_terminal(root)

    item = history.find_active_project_queue_item("me/game", 3)
    assert item is not None and item.state == "paused"
    assert [run.ticket for run in admitted] == [3]


def test_manual_board_removal_only_removes_not_started_ticket(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    coordinator, history, board, admitted, now = setup
    coordinator.add_batch(REPOSITORY, [3, 16])
    coordinator.scan_once()
    now[0] += 5
    coordinator.scan_once()
    board.items = [item for item in board.items if item.number != 16]
    coordinator.scan_once()
    now[0] += 5
    coordinator.scan_once()

    assert admitted[0].ticket == 3
    assert history.find_active_project_queue_item("me/game", 16) is None


def test_oldest_one_ticket_batch_precedes_later_multi_ticket_batch(tmp_path: Path) -> None:
    history = History("sqlite+pysqlite:///:memory:")
    for batch_id, tickets in (("old", [9]), ("new", [2, 3])):
        history.create_project_queue_batch(
            batch_id=batch_id,
            repo="me/game",
            source="ui",
            items=[
                ProjectQueueItemSpec(
                    ticket=ticket,
                    project_owner="me",
                    project_title="Game",
                    project_item_id=f"item-{ticket}",
                    title=f"Ticket {ticket}",
                    branch=f"feat/ticket-{ticket}_{ticket}",
                )
                for ticket in tickets
            ],
        )
        history.stabilize_project_queue_batch(batch_id)
    claimed = history.claim_project_queue_head()
    assert claimed is not None and claimed.ticket == 9


def test_poll_interval_backs_off_while_the_board_is_unchanged(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    """Board polling is the service's dominant GraphQL cost, and an idle board answers every
    poll identically — so an untouched board must stop asking at full rate."""
    coordinator, _history, _board, _admitted, _now = setup

    assert coordinator.poll_interval() == 5

    coordinator._idle_polls = 1
    assert coordinator.poll_interval() == 10
    coordinator._idle_polls = 2
    assert coordinator.poll_interval() == 20


def test_poll_interval_is_capped(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    """A quiet board must still be looked at, or a ticket added during a lull is stranded.

    Two independent limits bind here. The doubling count caps growth relative to the configured
    interval, and the absolute ceiling caps it in wall-clock terms; whichever is lower wins.
    """
    coordinator, _history, _board, _admitted, _now = setup

    # interval_s=5: four doublings (80s) is reached before the 300s ceiling.
    coordinator._idle_polls = 10_000
    assert coordinator.poll_interval() == 80

    # A larger configured interval hits the absolute ceiling instead of doubling past it.
    coordinator._interval_s = 60
    assert coordinator.poll_interval() == 300.0


def test_a_change_resets_the_backoff(
    setup: tuple[ProjectQueueCoordinator, History, FakeBoard, list[RunState], list[float]],
) -> None:
    """Work flowing means the next event is likely imminent — responsiveness must return at once,
    not decay back over several polls."""
    coordinator, _history, _board, _admitted, _now = setup
    coordinator._idle_polls = 4
    assert coordinator.poll_interval() > 5

    before = coordinator._revision
    coordinator._changed()

    assert coordinator._revision != before, "_changed must signal the poll loop"
    coordinator._idle_polls = 0
    assert coordinator.poll_interval() == 5
