"""Durable GitHub Project ticket scheduling and end-to-end merge ownership."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from quill.git_ops import GitError
from quill.project_board import (
    ProjectCatalog,
    ProjectIssueItem,
    ProjectMoveResult,
    derive_branch_name,
)
from quill_api.db import History, ProjectQueueItem, ProjectQueueItemSpec
from quill_api.repository_registry import ConfiguredRepository, ConfiguredRepositoryRegistry
from quill_api.schemas import (
    ProjectQueueBatchInfo,
    ProjectQueueBatchResult,
    ProjectQueueItemInfo,
    ProjectQueueAddResult,
    ProjectQueueView,
)
from quill_api.state import RunState, RunStatus, RunStore

#: Ceiling on the idle backoff. A board nobody is touching still gets looked at every few
#: minutes, so a ticket added while the service was quiet is never stranded indefinitely.
_MAX_IDLE_INTERVAL_S = 300.0
#: How many times the interval may double. Caps the growth independently of the ceiling so a
#: large configured interval cannot overshoot it in a single step.
_BACKOFF_DOUBLINGS = 4


class BoardBoundary(Protocol):
    def catalog(
        self, repo: str, board_title: str, excluded_labels: tuple[str, ...] = ()
    ) -> ProjectCatalog: ...

    def queue_items(
        self, repo: str, board_title: str, excluded_labels: tuple[str, ...] = ()
    ) -> tuple[ProjectIssueItem, ...]: ...

    def move_issues(
        self, repo: str, tickets: tuple[int, ...] | list[int], board_title: str, status: str
    ) -> tuple[ProjectMoveResult, ...]: ...

    def move_issue(self, repo: str, ticket: int, board_title: str, status: str) -> None: ...


type BoardFactory = Callable[[], BoardBoundary]
type AdmitRoot = Callable[[ProjectQueueItem], RunState]
type Publish = Callable[[], None]
type Idle = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class PullRequestState:
    """Exact GitHub state used to decide whether one queue ticket may release the next."""

    number: int
    state: str
    branch: str
    tickets: frozenset[int]


@dataclass(slots=True)
class _ObservedQueue:
    item_ids: tuple[str, ...]
    changed_at: float
    reconciled: bool = False


class ProjectQueueCoordinator:
    """Own the durable FIFO above Quill's single admitted-run queue."""

    def __init__(
        self,
        history: History,
        repositories: ConfiguredRepositoryRegistry,
        store: RunStore,
        board_factory: BoardFactory,
        admit_root: AdmitRoot,
        idle: Idle,
        publish: Publish,
        admission_lock: threading.Lock,
        *,
        interval_s: float = 5.0,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._history = history
        self._repositories = repositories
        self._store = store
        self._board = board_factory()
        self._admit_root = admit_root
        self._idle = idle
        self._publish = publish
        self._admission_lock = admission_lock
        self._interval_s = interval_s
        self._enabled = enabled
        self._clock = clock
        self._observed: dict[str, _ObservedQueue] = {}
        #: Bumped by :meth:`_changed`. The poll loop compares it across a scan to tell a poll that
        #: saw real board movement from one that spent GraphQL quota to learn nothing.
        self._revision = 0
        self._idle_polls = 0
        # REST mutations and the watcher share one cached Project client. Serializing their board
        # reads prevents a candidate refresh from racing a five-second reconciliation pass.
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Recover durable ownership, then begin bounded Project polling."""
        self.recover()
        if not self._enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="quill-project-queue", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def view(self) -> ProjectQueueView:
        """Return the ordered active snapshot used by REST, SSE, and Overview."""
        batches = self._history.list_project_queue_batches(active_only=True)
        items = self._history.list_project_queue_items(active_only=True)
        grouped: dict[str, list[ProjectQueueItem]] = {batch.batch_id: [] for batch in batches}
        for item in items:
            grouped.setdefault(item.batch_id, []).append(item)
        result = [
            ProjectQueueBatchInfo(
                batch_id=batch.batch_id,
                position=batch.position,
                repo=batch.repo,
                state=batch.state,
                submitted_at=batch.created_at,
                error=batch.error,
                items=[self._item_info(item) for item in grouped.get(batch.batch_id, [])],
            )
            for batch in batches
        ]
        return ProjectQueueView(batches=result, depth=len(items))

    def catalog(self, repository: ConfiguredRepository) -> ProjectCatalog:
        with self._lock:
            if repository.project_board is None:
                raise ValueError(f"{repository.name} does not configure a GitHub Project board")
            return self._board.catalog(
                repository.name,
                repository.project_board,
                repository.excluded_issue_labels,
            )

    def add_batch(
        self, repository: ConfiguredRepository, tickets: list[int]
    ) -> ProjectQueueBatchResult:
        """Validate and move one explicit selection, retaining successful partial mutations."""
        with self._lock:
            return self._add_batch_locked(repository, tickets)

    def _add_batch_locked(
        self, repository: ConfiguredRepository, tickets: list[int]
    ) -> ProjectQueueBatchResult:
        if repository.project_board is None:
            raise ValueError(f"{repository.name} does not configure a GitHub Project board")
        catalog = self.catalog(repository)
        by_ticket = {item.number: item for item in catalog.tickets}
        results: dict[int, ProjectQueueAddResult] = {}
        eligible: list[ProjectIssueItem] = []
        for ticket in sorted(tickets):
            item = by_ticket.get(ticket)
            active = self._history.find_active_project_queue_item(repository.name, ticket)
            if item is None:
                results[ticket] = ProjectQueueAddResult(
                    ticket=ticket, queued=False, reason="not an open selectable Project ticket"
                )
            elif active is not None:
                results[ticket] = ProjectQueueAddResult(
                    ticket=ticket,
                    queued=False,
                    reason=f"already belongs to queue batch {active.batch_id}",
                )
            elif not item.selectable:
                results[ticket] = ProjectQueueAddResult(
                    ticket=ticket,
                    queued=False,
                    reason=f"Project status is {item.status or 'unset'}",
                )
            else:
                eligible.append(item)

        moves = self._board.move_issues(
            repository.name,
            [item.number for item in eligible],
            repository.project_board,
            "Queue",
        )
        moved = {result.ticket: result for result in moves}
        successful: list[ProjectIssueItem] = []
        for item in eligible:
            result = moved[item.number]
            if result.success:
                successful.append(item)
                results[item.number] = ProjectQueueAddResult(ticket=item.number, queued=True)
            else:
                results[item.number] = ProjectQueueAddResult(
                    ticket=item.number,
                    queued=False,
                    reason=result.error or "GitHub Project update failed",
                )

        batch_id: str | None = None
        if successful:
            batch_id = uuid.uuid4().hex
            specs = [self._item_spec(repository, catalog, item) for item in successful]
            try:
                self._history.create_project_queue_batch(
                    batch_id=batch_id,
                    repo=repository.name,
                    source="ui",
                    items=specs,
                )
            except ValueError as exc:
                batch_id = None
                for item in successful:
                    results[item.number] = ProjectQueueAddResult(
                        ticket=item.number, queued=False, reason=str(exc)
                    )
            else:
                self._changed()
        return ProjectQueueBatchResult(
            batch_id=batch_id,
            results=[results[ticket] for ticket in sorted(results)],
        )

    def scan_once(self) -> None:
        """Observe Project Queue changes, stabilize them, and reconcile execution ownership."""
        now = self._clock()
        with self._lock:
            for repository in self._queue_repositories():
                try:
                    queued = self._board.queue_items(
                        repository.name,
                        repository.project_board or "",
                        repository.excluded_issue_labels,
                    )
                except (GitError, OSError, ValueError, subprocess.SubprocessError):
                    continue
                identity = tuple(item.item_id for item in queued)
                observed = self._observed.get(repository.name)
                if observed is None or observed.item_ids != identity:
                    self._observed[repository.name] = _ObservedQueue(identity, now)
                    queued_tickets = {item.number for item in queued}
                    for item in self._history.list_project_queue_items(active_only=True):
                        if (
                            item.repo == repository.name
                            and item.state == "stabilizing"
                            and item.ticket in queued_tickets
                        ):
                            self._history.observe_project_queue_batch(
                                item.batch_id, observed_at=now
                            )
                    continue
                if observed.reconciled or now - observed.changed_at < self._interval_s:
                    continue
                self._reconcile_stable(repository, queued, now)
                observed.reconciled = True
        self.reconcile_owned_work()
        self._admit_if_idle()

    def recover(self) -> None:
        """Fail closed after restart and restore only durable, verifiable ownership."""
        items = self._history.recover_project_queue()
        for item in items:
            if item.state == "running" and item.current_run_id:
                row = self._history.get(item.current_run_id)
                if row is not None and row.status in {"failed", "halted"}:
                    self._history.pause_project_queue_item(
                        item.item_id,
                        error=row.error or f"run {row.run_id} {row.status}",
                        run_id=row.run_id,
                    )
        self.reconcile_owned_work()
        self._admit_if_idle()

    def attach_run(self, state: RunState, *, root: bool = False) -> bool:
        """Attach an exact root/review/update/restart run to its durable ticket owner."""
        item = None
        if state.source_run_id:
            item = self._history.project_queue_item_for_run(state.source_run_id)
        if item is None:
            item = self._history.find_active_project_queue_item(state.repo, state.ticket)
        if item is None or item.branch != state.branch:
            return False
        if state.pr_number is not None and item.pr_number not in {None, state.pr_number}:
            return False
        attached = self._history.attach_project_queue_run(item.item_id, state.run_id, root=root)
        if attached and state.pr_number is not None:
            self._history.attach_project_queue_pr(item.item_id, state.pr_number)
        if attached:
            self._changed()
        return attached

    def on_run_terminal(self, state: RunState) -> None:
        """Retain or pause the queue head; only an exact merge can complete it."""
        item = self._history.project_queue_item_for_run(state.run_id)
        if item is None:
            return
        if state.status in {RunStatus.FAILED, RunStatus.HALTED}:
            self._history.pause_project_queue_item(
                item.item_id,
                error=state.error or f"run {state.run_id} {state.status.value}",
                run_id=state.run_id,
            )
            self._changed()
            return
        if state.status != RunStatus.DONE:
            return
        pr_number = state.pr_number or item.pr_number
        if pr_number is None:
            self._history.pause_project_queue_item(
                item.item_id,
                error=f"completed run {state.run_id} did not identify the ticket pull request",
                run_id=state.run_id,
            )
            self._changed()
            return
        self._history.attach_project_queue_pr(item.item_id, pr_number)
        self._reconcile_pr(item.item_id)
        self._changed()
        self._admit_if_idle()

    def reconcile_owned_work(self) -> None:
        """Recognize external/manual merges without trusting issue closure or run status."""
        for item in self._history.list_project_queue_items(active_only=True):
            if item.state in {"waiting_pr", "paused"} and item.pr_number is not None:
                self._reconcile_pr(item.item_id)

    def _reconcile_stable(
        self,
        repository: ConfiguredRepository,
        queued: tuple[ProjectIssueItem, ...],
        now: float,
    ) -> None:
        current = {item.number: item for item in queued}
        durable = {
            item.ticket: item
            for item in self._history.list_project_queue_items(active_only=True)
            if item.repo == repository.name
        }
        changed = False
        for ticket, item in durable.items():
            if item.state in {"stabilizing", "pending"} and ticket not in current:
                changed = self._history.remove_pending_project_queue_item(item.item_id) or changed
        for batch_id in {
            item.batch_id
            for ticket, item in durable.items()
            if item.state == "stabilizing" and ticket in current
        }:
            changed = (
                self._history.stabilize_project_queue_batch(batch_id, stabilized_at=now) is not None
                or changed
            )

        manual = [item for ticket, item in current.items() if ticket not in durable]
        if manual:
            catalog = self.catalog(repository)
            batch_id = uuid.uuid4().hex
            self._history.create_project_queue_batch(
                batch_id=batch_id,
                repo=repository.name,
                source="board",
                items=[self._item_spec(repository, catalog, item) for item in manual],
                created_at=now,
            )
            self._history.stabilize_project_queue_batch(batch_id, stabilized_at=now)
            changed = True
        if changed:
            self._changed()

    def _admit_if_idle(self) -> None:
        with self._admission_lock:
            if not self._idle():
                return
            claimed = self._history.claim_project_queue_head()
            if claimed is None:
                return
            try:
                state = self._admit_root(claimed)
            except (GitError, OSError, RuntimeError, ValueError) as exc:
                self._history.pause_project_queue_item(claimed.item_id, error=str(exc))
            else:
                self._history.attach_project_queue_run(claimed.item_id, state.run_id, root=True)
        self._changed()

    def _reconcile_pr(self, item_id: str) -> None:
        item = next(
            (
                candidate
                for candidate in self._history.list_project_queue_items(active_only=True)
                if candidate.item_id == item_id
            ),
            None,
        )
        if item is None or item.pr_number is None:
            return
        try:
            pull = read_pull_request(item.repo, item.pr_number)
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        if (
            pull.number != item.pr_number
            or pull.branch != item.branch
            or item.ticket not in pull.tickets
        ):
            self._history.pause_project_queue_item(
                item.item_id,
                error=f"PR #{item.pr_number} no longer matches {item.repo}#{item.ticket} on {item.branch}",
            )
            self._changed()
            return
        if pull.state == "MERGED":
            try:
                self._board.move_issue(item.repo, item.ticket, item.project_title, "Done")
            except (GitError, OSError, subprocess.SubprocessError) as exc:
                self._history.pause_project_queue_item(
                    item.item_id, error=f"merged PR verified but Project update failed: {exc}"
                )
                self._changed()
                return
            self._history.complete_project_queue_item(item.item_id, pr_number=item.pr_number)
            self._changed()
        elif pull.state != "OPEN":
            self._history.pause_project_queue_item(
                item.item_id,
                error=f"PR #{item.pr_number} is {pull.state.lower()} without a verified merge",
            )
            self._changed()

    def _item_spec(
        self,
        repository: ConfiguredRepository,
        catalog: ProjectCatalog,
        item: ProjectIssueItem,
    ) -> ProjectQueueItemSpec:
        return ProjectQueueItemSpec(
            ticket=item.number,
            project_owner=catalog.project.owner,
            project_title=catalog.project.title,
            project_item_id=item.item_id,
            title=item.title,
            branch=derive_branch_name(
                item.number,
                item.title,
                item.labels,
                repository.excluded_issue_labels,
            ),
            epic_number=item.parent_number,
            epic_title=item.parent_title,
            workflow=repository.default_workflow,
            last_board_status="Queue",
        )

    def _queue_repositories(self) -> tuple[ConfiguredRepository, ...]:
        return tuple(
            repository
            for repository in self._repositories.repositories
            if repository.project_board is not None
        )

    def _changed(self) -> None:
        self._revision += 1
        self._publish()

    def poll_interval(self) -> float:
        """Seconds to wait before the next scan, backing off while the board sits still.

        Board polling is the service's dominant GraphQL cost, and an idle board answers every
        poll identically. Doubling the wait after each unchanged scan keeps the responsive
        interval for the case that matters — a ticket moved, a run just finished — while an
        untouched board settles to :data:`_MAX_IDLE_INTERVAL_S` instead of asking forever at
        full rate. Any change resets it immediately.
        """
        if self._idle_polls <= 0:
            return float(self._interval_s)
        backed_off = float(self._interval_s) * float(2 ** min(self._idle_polls, _BACKOFF_DOUBLINGS))
        return min(backed_off, _MAX_IDLE_INTERVAL_S)

    def _run(self) -> None:
        while not self._stopping.wait(self.poll_interval()):
            before = self._revision
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001 - one poll must never kill durable scheduling
                continue
            # A scan that changed nothing is evidence the board is quiet; one that did means work
            # is flowing and the next event is likely imminent.
            self._idle_polls = 0 if self._revision != before else self._idle_polls + 1

    def _item_info(self, item: ProjectQueueItem) -> ProjectQueueItemInfo:
        board_status = item.last_board_status or ""
        if item.state in {"stabilizing", "pending"}:
            board_status = "Queue"
        elif item.state == "waiting_pr" or (item.state == "paused" and item.pr_number):
            board_status = "In review"
        elif item.state == "running":
            current = self._history.get(item.current_run_id) if item.current_run_id else None
            board_status = (
                "In review" if current is not None and current.mode == "review" else "In progress"
            )
        return ProjectQueueItemInfo(
            ticket=item.ticket,
            title=item.title,
            epic_number=item.epic_number,
            epic_title=item.epic_title,
            position=item.position,
            state=item.state,
            board_status=board_status,
            run_id=item.current_run_id,
            pr_number=item.pr_number,
            error=item.error,
        )


def read_pull_request(repo: str, number: int) -> PullRequestState:
    """Read the exact PR identity and merge state; no ticket-search fallback is allowed."""
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,state,headRefName,closingIssuesReferences",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or f"could not read {repo} PR #{number}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned invalid pull-request metadata")
    raw_number = payload.get("number")
    state = payload.get("state")
    branch = payload.get("headRefName")
    issues = payload.get("closingIssuesReferences")
    if not isinstance(raw_number, int) or not isinstance(state, str) or not isinstance(branch, str):
        raise ValueError("GitHub returned incomplete pull-request metadata")
    if not isinstance(issues, list):
        raise ValueError("GitHub returned invalid closing-issue metadata")
    tickets = frozenset(
        issue["number"]
        for issue in issues
        if isinstance(issue, dict) and isinstance(issue.get("number"), int)
    )
    return PullRequestState(raw_number, state.upper(), branch, tickets)
