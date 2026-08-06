"""Process-wide singletons, wired once at startup.

What lives here is strictly machine-level: the queue, the run store, the event bus, the history DB,
the workspace manager and the catalog roots. Anything repo-specific — the runner CLI, the model
server, the git reader, build/test — is built **per run** by :class:`~quill_api.runner.RunManager`,
because it is a property of the checkout that run prepared, not of the process.

That split is the whole difference between "a server for one repo" and "a server".
"""

from __future__ import annotations

import threading
from dataclasses import asdict

from quill import events
from quill.eventlog import EventLog
from quill.git_ops import SubprocessRunner, pr_target_for_repo
from quill.mechanical import PR_REVIEW_NAME, _load_pr_review, pr_review_digest
from quill.pipeline import make_run_id
from quill.pipeline import run_pipeline
from quill.project_board import ProjectBoard
from quill.telemetry import SCHEMA_VERSION, build_breakdown
from quill_api.catalog_git import CatalogRepo
from quill_api.db import History, PrFeedbackCycle, ProjectQueueItem, RunRow
from quill_api.events import EventBus
from quill_api.pr_watcher import PullRequestWatcher, ReviewCandidate
from quill_api.project_queue import ProjectQueueCoordinator
from quill_api.queue import QueuedRun, RunQueue
from quill_api.repository_registry import ConfiguredRepositoryRegistry
from quill_api.runner import RunManager
from quill_api.settings import Settings
from quill.modelserver import VllmServer
from quill_api.model_registry import ModelSwitcher, ServiceModelRegistry
from quill_api.state import RunState, RunStatus, RunStore
from quill_api.projections import queue_view, run_summary
from quill_api.telemetry import (
    LinuxTelemetryReader,
    ModelSwitchTelemetry,
    SystemTelemetryMonitor,
    VllmThroughputSampler,
)
from quill_api.workspace import (
    WorkspaceManager,
    WorkspaceNotFound,
    validate_branch,
    validate_repo,
)


class Services:
    """Everything the routes reach for, assembled from :class:`Settings`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        # Directories are created here rather than lazily because History opens its database
        # immediately below, and that database lives in the state dir.
        self.settings.ensure_dirs()

        self.store = RunStore()
        self.run_admission_lock = threading.Lock()
        self.bus = EventBus()
        self.history = History(self.settings.db_url)
        self.repositories = ConfiguredRepositoryRegistry(
            self.settings.state_dir / "configured-repositories.json",
            on_refreshed=self._publish_repositories_refreshed,
        )
        self.workspaces = WorkspaceManager(
            self.settings.workspace_root,
            git_author=(self.settings.git_author_name, self.settings.git_author_email),
        )
        self.personas = CatalogRepo(self.settings.personas_root)
        self.skills = CatalogRepo(self.settings.skills_root)
        self.model_registry = ServiceModelRegistry()
        self.model_switcher = ModelSwitcher(
            server_factory=lambda: VllmServer(self.settings.vllm_url),
            command=self.settings.vllm_switch_command,
            stop_command=self.settings.vllm_stop_command,
        )
        self.telemetry = SystemTelemetryMonitor(
            LinuxTelemetryReader(
                VllmThroughputSampler(self.settings.vllm_url),
                cpu_fan_hwmon_name=self.settings.cpu_fan_hwmon_name,
                cpu_fan_pwm_channel=self.settings.cpu_fan_pwm_channel,
            ),
            self.settings.telemetry_interval_s,
            switch_state=lambda: ModelSwitchTelemetry(**asdict(self.model_switcher.state)),
        )

        self.manager = RunManager(
            self.store,
            self.bus,
            self.workspaces,
            run_pipeline=run_pipeline,
            runs_root=self.settings.runs_root,
            personas_root=self.settings.personas_root,
            vllm_url=self.settings.vllm_url,
            history_record=self.history.record,
            breakdown_record=self.history.record_breakdown,
            on_terminal=self._on_run_terminal,
            usd_per_1m=self.settings.usd_per_1m_tokens,
        )
        self.queue = RunQueue(self.manager.execute)
        self.project_queue = ProjectQueueCoordinator(
            self.history,
            self.repositories,
            self.store,
            board_factory=lambda: ProjectBoard(SubprocessRunner(str(self.settings.workspace_root))),
            admit_root=self._admit_project_queue_root_locked,
            idle=lambda: self.store.active is None and self.queue.depth == 0,
            publish=self._publish_project_queue,
            admission_lock=self.run_admission_lock,
            interval_s=self.settings.project_queue_watch_interval_s,
            enabled=self.settings.project_queue_watch_enabled,
        )
        self.pr_watcher = PullRequestWatcher(
            self.repositories,
            self._admit_pr_review,
            interval_s=self.settings.pr_watch_interval_s,
            enabled=self.settings.pr_watch_enabled,
            maintain=self._maintain_pr_feedback_loop,
        )
        self.manager._live_publish = (
            self._publish_run
        )  # coordinator wiring; tests still inject manager
        self.queue.set_on_change(self._publish_queue)

    def live_sync(self) -> dict[str, object]:
        return {
            "type": "sync",
            "runs": [
                run_summary(run, self.queue.position).model_dump() for run in self.store.all()
            ],
            "queue": queue_view(self.store, self.queue.position, self.queue.depth).model_dump(),
            "project_queue": self.project_queue.view().model_dump(),
        }

    def _publish_run(self, event: dict[str, object], state: object) -> None:
        from quill_api.state import RunState

        assert isinstance(state, RunState)
        self.bus.publish_threadsafe(
            {
                **event,
                "run_id": state.run_id,
                "run": run_summary(state, self.queue.position).model_dump(),
            }
        )

    def _publish_queue(self) -> None:
        if not self.bus.is_bound:
            return
        self.bus.publish_threadsafe(
            {
                "type": "queue_updated",
                "queue": queue_view(self.store, self.queue.position, self.queue.depth).model_dump(),
            }
        )

    def _publish_project_queue(self) -> None:
        if not self.bus.is_bound:
            return
        self.bus.publish_threadsafe(
            {
                "type": "project_queue_updated",
                "project_queue": self.project_queue.view().model_dump(),
            }
        )

    def _publish_repositories_refreshed(self) -> None:
        """Announce that background repository discovery produced a new snapshot.

        ``GET /github/repositories`` answers from cache and rescans behind the response, so
        without this a client that read during a cold window never learned the newer list.
        The payload is deliberately a bare tag: clients refetch the endpoint rather than trust
        a serialized list pushed from a worker thread.
        """
        if not self.bus.is_bound:
            return
        self.bus.publish_threadsafe({"type": "repositories_refreshed"})

    def start(self) -> None:
        """Begin draining the queue and close out runs stranded by the last shutdown."""
        for run_id in self.history.reconcile_orphans():
            row = self.history.get(run_id)
            if row is None:
                continue
            data: dict[str, object] = {
                "status": row.status,
                "repo": row.repo,
                "branch": row.branch,
                "ticket": row.ticket,
                "backend": None,
                "started_at": row.started_at,
                "updated_at": row.finished_at,
                "error": row.error,
            }
            breakdown = build_breakdown(
                run_id,
                self.settings.runs_root / run_id,
                data,
                usd_per_1m=self.settings.usd_per_1m_tokens,
            )
            self.history.record_breakdown(run_id, breakdown, SCHEMA_VERSION)
        if self.settings.pr_feedback_loop_enabled:
            self._maintain_pr_feedback_loop()
        self.repositories.refresh_async()
        self.telemetry.start()
        self.queue.start()
        self.project_queue.start()
        self.pr_watcher.start()

    def stop(self) -> None:
        self.pr_watcher.stop()
        self.project_queue.stop()
        self.queue.stop()
        self.telemetry.stop()

    def _admit_pr_review(self, candidate: ReviewCandidate) -> bool:
        """Persist and queue one automatic review for an exact PR head."""
        with self.run_admission_lock:
            if self.history.has_pr_review(candidate.repo, candidate.pr_number, candidate.head_sha):
                return False
            run_id = make_run_id(candidate.ticket)
            base_run_id = run_id
            suffix = 2
            while self.store.get(run_id) is not None or self.history.get(run_id) is not None:
                run_id = f"{base_run_id}-{suffix}"
                suffix += 1
            state = RunState(
                run_id=run_id,
                ticket=candidate.ticket,
                repo=candidate.repo,
                branch=candidate.branch,
                mode="review",
                workflow="pr_review",
                pr_number=candidate.pr_number,
                pr_head_sha=candidate.head_sha,
            )
            self.store.add(state)
            self.history.record(state)
            self.project_queue.attach_run(state)
            with EventLog(self.settings.runs_root / run_id) as event_log:
                event_log.append(
                    events.run_queued(
                        run_id,
                        candidate.ticket,
                        repo=candidate.repo,
                        branch=candidate.branch,
                        mode="review",
                        workflow="pr_review",
                    )
                )
            self.queue.submit(
                QueuedRun(
                    run_id=run_id,
                    repo=candidate.repo,
                    branch=candidate.branch,
                    ticket=candidate.ticket,
                    mode="review",
                    workflow="pr_review",
                    pr_number=candidate.pr_number,
                    pr_head_sha=candidate.head_sha,
                )
            )
        return True

    def _on_run_terminal(self, state: RunState) -> None:
        """Reconcile durable ticket ownership and Quill-owned PR feedback work."""
        self.project_queue.on_run_terminal(state)
        if not self.settings.pr_feedback_loop_enabled:
            return
        if (
            state.mode == "review"
            and state.workflow == "pr_review"
            and state.status.value == "done"
        ):
            self._record_pr_review_result(
                run_id=state.run_id,
                repo=state.repo,
                branch=state.branch or "",
                ticket=state.ticket,
                pr_number=state.pr_number,
                head_sha=state.pr_head_sha,
            )
            return
        if state.mode != "update":
            return
        cycle = self.history.feedback_cycle_for_update(state.run_id)
        if cycle is None:
            return
        if state.status.value != "done":
            self.history.finish_pr_feedback_update(
                state.run_id,
                status=f"update_{state.status.value}",
                error=state.error,
            )
            return
        self._reconcile_completed_pr_feedback_update(state.run_id, cycle)

    def _reconcile_completed_pr_feedback_update(
        self, update_run_id: str, cycle: PrFeedbackCycle
    ) -> None:
        target = pr_target_for_repo(
            SubprocessRunner(str(self.settings.workspace_root)), cycle.repo, cycle.ticket
        )
        if target is None:
            self.history.finish_pr_feedback_update(
                update_run_id,
                status="update_failed",
                error=f"PR #{cycle.pr_number} was not open after the update completed",
            )
        elif target.head_sha == cycle.reviewed_head_sha:
            self.history.finish_pr_feedback_update(
                update_run_id,
                status="update_no_change",
                resulting_head_sha=target.head_sha,
                error="the update completed without pushing a new PR head",
            )
        else:
            self.history.finish_pr_feedback_update(
                update_run_id,
                status="awaiting_review",
                resulting_head_sha=target.head_sha,
            )

    def _record_pr_review_result(
        self,
        *,
        run_id: str,
        repo: str,
        branch: str,
        ticket: int,
        pr_number: int | None,
        head_sha: str | None,
    ) -> None:
        """Validate a published result, persist it, and dispatch a BLOCK exactly once."""
        if pr_number is None or not branch or not head_sha:
            return
        try:
            review = _load_pr_review(self.settings.runs_root / run_id / PR_REVIEW_NAME)
        except (OSError, ValueError):
            return
        cycle = self.history.record_pr_feedback_result(
            review_run_id=run_id,
            repo=repo,
            pr_number=pr_number,
            ticket=ticket,
            branch=branch,
            reviewed_head_sha=head_sha,
            findings_digest=pr_review_digest(review),
            verdict=str(review["verdict"]),
            max_cycles=self.settings.pr_feedback_loop_max_cycles,
        )
        if cycle.status != "update_pending":
            return
        target = pr_target_for_repo(
            SubprocessRunner(str(self.settings.workspace_root)), repo, ticket
        )
        if target is None or target.number != pr_number or target.head_sha != head_sha:
            self.history.finish_pr_feedback_cycle(
                run_id,
                status="superseded",
                error="the PR head changed before its automatic update was dispatched",
            )
            return
        self._queue_pr_feedback_update(cycle)

    def _queue_pr_feedback_update(self, cycle: PrFeedbackCycle) -> bool:
        """Claim and queue one update run for a persisted BLOCK decision."""
        with self.run_admission_lock:
            run_id = self._unique_run_id(cycle.ticket)
            if not self.history.attach_pr_feedback_update(cycle.review_run_id, run_id):
                return False
            state = RunState(
                run_id=run_id,
                ticket=cycle.ticket,
                repo=cycle.repo,
                branch=cycle.branch,
                mode="update",
                workflow="pr_update",
                pr_number=cycle.pr_number,
                pr_head_sha=cycle.reviewed_head_sha,
                feedback_digest=cycle.findings_digest,
            )
            self.store.add(state)
            self.history.record(state)
            self.project_queue.attach_run(state)
            with EventLog(self.settings.runs_root / run_id) as event_log:
                event_log.append(
                    events.run_queued(
                        run_id,
                        cycle.ticket,
                        repo=cycle.repo,
                        branch=cycle.branch,
                        mode="update",
                        workflow="pr_update",
                    )
                )
            self.queue.submit(
                QueuedRun(
                    run_id=run_id,
                    repo=cycle.repo,
                    branch=cycle.branch,
                    ticket=cycle.ticket,
                    mode="update",
                    workflow="pr_update",
                    pr_number=cycle.pr_number,
                    pr_head_sha=cycle.reviewed_head_sha,
                    feedback_digest=cycle.findings_digest,
                )
            )
            return True

    def _maintain_pr_feedback_loop(self) -> None:
        """Retry durable feedback work on startup and every PR-watcher tick."""
        if not self.settings.pr_feedback_loop_enabled:
            return
        for row in self.history.untracked_completed_pr_reviews():
            self._record_review_row(row)
        for cycle in self.history.recoverable_pr_feedback_cycles():
            self._queue_pr_feedback_update(cycle)
        for row in self.history.unreconciled_completed_pr_feedback_updates():
            completed_cycle = self.history.feedback_cycle_for_update(row.run_id)
            if completed_cycle is not None:
                self._reconcile_completed_pr_feedback_update(row.run_id, completed_cycle)

    def _record_review_row(self, row: RunRow) -> None:
        self._record_pr_review_result(
            run_id=row.run_id,
            repo=row.repo or "",
            branch=row.branch or "",
            ticket=row.ticket,
            pr_number=row.pr_number,
            head_sha=row.pr_head_sha,
        )

    def _unique_run_id(self, ticket: int) -> str:
        run_id = make_run_id(ticket)
        base_run_id = run_id
        suffix = 2
        while self.store.get(run_id) is not None or self.history.get(run_id) is not None:
            run_id = f"{base_run_id}-{suffix}"
            suffix += 1
        return run_id

    def _admit_project_queue_root_locked(self, item: ProjectQueueItem) -> RunState:
        """Admit a claimed project ticket while the coordinator holds the global lock."""
        return self._admit_run_locked(
            repo=item.repo,
            branch=item.branch,
            ticket=item.ticket,
            mode="create",
            workflow=item.workflow,
            clear_prefix_cache=False,
            model_overrides=(),
        )

    def admit_run(
        self,
        *,
        repo: str,
        branch: str,
        ticket: int,
        mode: str = "create",
        workflow: str = "ticket",
        clear_prefix_cache: bool = False,
        model_overrides: tuple[tuple[str, str], ...] = (),
    ) -> RunState:
        """Validate, persist, and submit one create run under the global admission lock.

        HTTP starts and the durable Project scheduler intentionally share this path. Keeping branch
        replacement, run IDs, history, and the queue event in one place prevents unattended work
        from becoming a privileged second implementation of run admission.
        """
        canonical_repo = validate_repo(repo)
        canonical_branch = validate_branch(branch)
        owned_ticket = self.history.find_active_project_queue_item(canonical_repo, ticket)
        if owned_ticket is not None:
            raise RuntimeError(
                f"{canonical_repo}#{ticket} already belongs to project queue batch "
                f"{owned_ticket.batch_id}"
            )
        with self.run_admission_lock:
            return self._admit_run_locked(
                repo=canonical_repo,
                branch=canonical_branch,
                ticket=ticket,
                mode=mode,
                workflow=workflow,
                clear_prefix_cache=clear_prefix_cache,
                model_overrides=model_overrides,
            )

    def _admit_run_locked(
        self,
        *,
        repo: str,
        branch: str,
        ticket: int,
        mode: str,
        workflow: str,
        clear_prefix_cache: bool,
        model_overrides: tuple[tuple[str, str], ...],
    ) -> RunState:
        """Admission implementation for callers already holding ``run_admission_lock``."""
        existing = self.store.active
        if existing is not None or self.queue.depth:
            active_id = existing.run_id if existing is not None else "queued run"
            raise RuntimeError(f"Quill already has an active run ({active_id})")

        try:
            local_exists = mode == "create" and branch in self.workspaces.local_branches(repo)
        except WorkspaceNotFound:
            local_exists = False
        if mode == "create" and local_exists:
            owned_terminal = any(
                item.repo == repo
                and item.branch == branch
                and item.mode == "create"
                and item.status in {RunStatus.FAILED, RunStatus.HALTED}
                and not item.pr_url
                and item.pr_number is None
                for item in self.store.all()
            )
            if not owned_terminal:
                owned_terminal = any(
                    item.branch == branch
                    and item.mode == "create"
                    and item.status in {"failed", "halted"}
                    and not item.pr_url
                    and item.pr_number is None
                    for item in self.history.recent(limit=200, repo=repo)
                )
            if not owned_terminal or self.workspaces.branch_has_pull_request(repo, branch):
                raise RuntimeError(
                    f"branch '{branch}' already exists locally and cannot be replaced"
                )
            self.workspaces.discard_run_branch(repo, branch, base="main")

        run_id = self._unique_run_id(ticket)
        state = RunState(
            run_id=run_id,
            ticket=ticket,
            repo=repo,
            branch=branch,
            mode=mode,
            workflow=workflow,
            clear_prefix_cache=clear_prefix_cache,
        )
        self.store.add(state)
        self.history.record(state)
        with EventLog(self.settings.runs_root / run_id) as event_log:
            event_log.append(
                events.run_queued(
                    run_id,
                    ticket,
                    repo=repo,
                    branch=branch,
                    mode=mode,
                    workflow=workflow,
                    model_overrides=dict(model_overrides) or None,
                )
            )
        self.queue.submit(
            QueuedRun(
                run_id=run_id,
                repo=repo,
                branch=branch,
                ticket=ticket,
                mode=mode,
                workflow=workflow,
                clear_prefix_cache=clear_prefix_cache,
                model_overrides=model_overrides,
            )
        )
        return state
