"""RunManager — prepares a workspace, drives one pipeline run, folds its events (WI-10).

The API owns the runs. The queue decides *when* one executes; this decides *how*:

1. materialise the repo at the requested branch (:mod:`quill_api.workspace`),
2. load the config committed at the root of that checkout,
3. build the pipeline's dependencies **for that repo**, and
4. drive ``run_pipeline``, folding every event into the live `RunState` and fanning it out to SSE
   subscribers through the `EventBus`.

Step 3 is the reason this class exists in its current shape. Dependencies used to be built once at
startup against ``"."`` — the directory the service happened to be launched from — which is what
made the old API "one server per repo". They are per-run now: the runner, model server, git reader
and build/test runner are all bound to the workspace this particular run prepared.

Stop flags and pending decisions are keyed by run id, since a queue means more than one run exists
at a time even though only one executes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from quill import events
from quill.checkpoints import CheckpointRecorder, load_manifest
from quill.failure_codes import classify_failure, classify_terminal_failure
from quill.config import ConfigError, PhaseDef, QuillfolioConfig, load_config
from quill.eventlog import EventLog
from quill.events import Event
from quill.git_ops import GitError, GitOps, SubprocessRunner, pr_target_for_repo
from quill.live_usage import LiveUsage
from quill.mechanical import build_test_runner
from quill.modelserver import make_model_server
from quill.modelserver import VllmServer
from quill.project_board import ProjectBoard
from quill.pipeline import PipelineDeps
from quill.restart import RestartError, prepare_contract_restart
from quill.runners import UnknownRunnerError, get_runner
from quill.telemetry import (
    SCHEMA_VERSION,
    build_breakdown,
    cumulative_live_usage,
    phase_window_usage,
    token_cost,
)
from quill_api.events import EventBus
from quill_api.queue import QueuedRun
from quill_api.state import RunState, RunStatus, RunStore
from quill_api.workspace import WorkspaceError, WorkspaceManager

#: Signature of the pipeline entry point, injected so tests pass a fake.
type RunPipeline = Callable[..., Event]
type RunTerminal = Callable[[RunState], None]


def apply_model_overrides(
    config: QuillfolioConfig, overrides: tuple[tuple[str, str], ...]
) -> QuillfolioConfig:
    """Return a run-local config with validated per-phase model substitutions."""
    if not overrides:
        return config
    requested = dict(overrides)
    known_phases = {phase.id for phase in config.phases}
    unknown = sorted(requested.keys() - known_phases)
    if unknown:
        raise ConfigError(f"model override names unknown phase(s): {', '.join(unknown)}")
    available = set(config.vllm_models)
    for phase in config.phases:
        available.update(phase.models)
        available.update(audit.model for audit in phase.audits)
    invalid_models = sorted(set(requested.values()) - available)
    if invalid_models:
        raise ConfigError(f"model override names unavailable model(s): {', '.join(invalid_models)}")

    phases: list[PhaseDef] = []
    for phase in config.phases:
        model = requested.get(phase.id)
        if model is None:
            phases.append(phase)
        elif phase.audits:
            phases.append(
                replace(
                    phase,
                    audits=tuple(replace(audit, model=model) for audit in phase.audits),
                )
            )
        elif phase.models:
            phases.append(replace(phase, models=(model,)))
        else:
            raise ConfigError(
                f"phase '{phase.id}' does not execute a model and cannot be overridden"
            )
    parallel_models: dict[str, set[str | None]] = {}
    for phase in phases:
        if phase.parallel_group is not None:
            parallel_models.setdefault(phase.parallel_group, set()).add(phase.model)
    inconsistent = [group for group, models in parallel_models.items() if len(models) != 1]
    if inconsistent:
        raise ConfigError(
            "model overrides must keep each parallel producer group on one model: "
            + ", ".join(sorted(inconsistent))
        )
    workflows = dict(config.workflows)
    selected_workflow = workflows.get(config.workflow_id)
    if selected_workflow is not None:
        workflows[config.workflow_id] = replace(selected_workflow, phases=tuple(phases))
    return replace(config, phases=phases, workflows=workflows)


@dataclass(slots=True)
class _Decision:
    event: threading.Event
    answer: str | None = None


@dataclass(slots=True)
class _RunControls:
    """Per-run stop flag and pending decision. Keyed by run id — a queue means several coexist."""

    stop: threading.Event = field(default_factory=threading.Event)
    decision: _Decision | None = None
    cancel_active: Callable[[], None] | None = None


@dataclass(slots=True)
class _LiveUsageAttribution:
    """Keep phase-cumulative usage separate from the active execution's usage."""

    inherited: dict[str, LiveUsage]
    phase_usage: dict[str, LiveUsage] = field(init=False)
    active_execution_usage: dict[str, LiveUsage] = field(default_factory=dict)
    execution_baselines: dict[str, LiveUsage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.phase_usage = dict(self.inherited)

    def start_execution(self, phase: str) -> None:
        self.execution_baselines[phase] = self.phase_usage.get(phase, LiveUsage())
        self.active_execution_usage[phase] = LiveUsage()

    def update(self, phase: str, snapshot: LiveUsage) -> None:
        inherited = self.inherited.get(phase, LiveUsage())
        cumulative = LiveUsage(
            inherited.input_tokens + snapshot.input_tokens,
            inherited.output_tokens + snapshot.output_tokens,
            inherited.context_window_tokens + snapshot.context_window_tokens,
        )
        # A progress snapshot is cumulative for this phase, including earlier retries. The
        # execution baseline is captured by PHASE_STARTED so the table row remains attempt-local.
        baseline = self.execution_baselines.get(phase, inherited)
        self.phase_usage[phase] = cumulative
        self.active_execution_usage[phase] = LiveUsage(
            max(0, cumulative.input_tokens - baseline.input_tokens),
            max(0, cumulative.output_tokens - baseline.output_tokens),
            max(0, cumulative.context_window_tokens - baseline.context_window_tokens),
        )


class RunManager:
    """Executes one queued run: workspace → config → deps → pipeline."""

    def __init__(
        self,
        store: RunStore,
        bus: EventBus,
        workspaces: WorkspaceManager,
        *,
        run_pipeline: RunPipeline,
        runs_root: Path,
        personas_root: Path,
        vllm_url: str,
        history_record: Callable[[RunState], None] | None = None,
        breakdown_record: Callable[[str, dict[str, object], int], None] | None = None,
        live_publish: Callable[[Event, RunState], None] | None = None,
        on_terminal: RunTerminal | None = None,
        usd_per_1m: float = 0.043,
    ) -> None:
        self._store = store
        self._bus = bus
        self._workspaces = workspaces
        self._run_pipeline = run_pipeline
        self._runs_root = runs_root
        self._personas_root = personas_root
        self._vllm_url = vllm_url
        self._usd_per_1m = usd_per_1m
        self._history_record = history_record
        self._breakdown_record = breakdown_record
        self._live_publish = live_publish
        self._on_terminal = on_terminal
        self._controls: dict[str, _RunControls] = {}
        self._lock = threading.Lock()

    # -- control surface ----------------------------------------------------------

    def request_stop(self, run_id: str) -> bool:
        """Stop a run, cancelling its active agent process when one is executing."""
        state = self._store.get(run_id)
        if state is None:
            return False
        controls = self._controls_for(run_id)
        controls.stop.set()
        cancel_active = controls.cancel_active
        if cancel_active is not None:
            cancel_active()
        return True

    def cancel_queued(self, run_id: str) -> bool:
        """Mark a still-queued run halted immediately."""
        state = self._store.get(run_id)
        if state is None or state.status is not RunStatus.QUEUED:
            return False
        self._finish(state, RunStatus.HALTED, "stopped before starting")
        return True

    def answer_decision(self, run_id: str, answer: str) -> bool:
        """Feed an answer to a parked run. False if nothing is awaiting one."""
        controls = self._controls.get(run_id)
        decision = controls.decision if controls else None
        if decision is None:
            return False
        decision.answer = answer
        decision.event.set()
        return True

    # -- execution ----------------------------------------------------------------

    def execute(self, queued: QueuedRun) -> None:
        """Run ``queued`` to completion, recording everything on its `RunState`."""
        state = self._store.get(queued.run_id)
        if state is None:  # submitted without a state — nothing to report progress on
            return
        controls = self._controls_for(queued.run_id)
        if controls.stop.is_set():
            # Stopped while queued: never start the work at all.
            self._finish(state, RunStatus.HALTED, "stopped before starting")
            return

        state.mark_started()
        try:
            if queued.mode == "review":
                target = pr_target_for_repo(
                    SubprocessRunner(str(self._workspaces.root)), queued.repo, queued.ticket
                )
                if target is None:
                    raise WorkspaceError(f"no open PR found for ticket #{queued.ticket}")
                if target.branch != queued.branch:
                    raise WorkspaceError(
                        f"PR #{target.number} now targets '{target.branch}', not requested "
                        f"'{queued.branch}'; refresh the run form"
                    )
                if queued.pr_head_sha is not None and target.head_sha != queued.pr_head_sha:
                    raise WorkspaceError(
                        f"PR #{target.number} moved from queued head {queued.pr_head_sha[:12]} "
                        f"to {target.head_sha[:12]}; the watcher will review the new head after checks"
                    )
                state.pr_number = target.number
            elif queued.mode == "update":
                local = self._workspaces.local_branches(queued.repo)
                git = GitOps(SubprocessRunner(str(self._workspaces.path_for(queued.repo))))
                target = git.pr_target_for_ticket(queued.ticket)
                if target is None:
                    raise WorkspaceError(f"no open PR found for ticket #{queued.ticket}")
                if target.branch != queued.branch:
                    raise WorkspaceError(
                        f"PR #{target.number} now targets '{target.branch}', not requested "
                        f"'{queued.branch}'; refresh the run form"
                    )
                if target.branch not in local:
                    raise WorkspaceError(
                        f"PR #{target.number} branch '{target.branch}' is not a local branch"
                    )
                if queued.pr_head_sha is not None and target.head_sha != queued.pr_head_sha:
                    raise WorkspaceError(
                        f"PR #{target.number} moved from reviewed head "
                        f"{queued.pr_head_sha[:12]} to {target.head_sha[:12]}; automatic update "
                        "cancelled because its feedback is stale"
                    )
                state.pr_number = target.number
            if queued.checkpoint_commit is not None:
                restart_manifest = load_manifest(self._runs_root / (queued.source_run_id or ""))
                if restart_manifest is None:
                    raise WorkspaceError("source run has no valid phase checkpoints")
                restart_status = self._workspaces.restart_status(
                    queued.repo,
                    queued.branch,
                    base="main",
                    checkpoint_base=restart_manifest.base,
                )
                if not restart_status.eligible:
                    raise WorkspaceError(
                        restart_status.reason or "branch is no longer eligible for restart"
                    )
                config_workspace = self._workspaces.prepare_default_for_config(queued.repo)
            elif queued.mode == "review":
                config_workspace = self._workspaces.prepare_default_for_config(queued.repo)
            else:
                config_workspace = self._workspaces.prepare_for_config(queued.repo, queued.branch)
            if queued.mode == "create" and config_workspace.requested_branch_exists:
                raise WorkspaceError(
                    f"branch '{queued.branch}' already exists on origin; use an update workflow"
                )
        except WorkspaceError as exc:
            self._finish(state, RunStatus.FAILED, str(exc))
            return

        run_dir = self._runs_root / queued.run_id
        try:
            config = load_config(
                config_workspace.workspace.path,
                personas_root=self._personas_root,
                runs_root=self._runs_root,
                vllm_url=self._vllm_url,
            )
            config = config.select_workflow(queued.workflow)
            workflow = config.workflow(queued.workflow)
            if workflow is None or workflow.mode != queued.mode:
                raise ConfigError(
                    f"workflow '{queued.workflow}' does not support mode '{queued.mode}'."
                )
            workspace = config_workspace.workspace
            if queued.checkpoint_commit is not None:
                if queued.start_phase not in config.phase_ids:
                    raise ConfigError(
                        f"restart phase '{queued.start_phase}' is not in the current workflow"
                    )
                workspace = self._workspaces.restore_run_checkpoint(
                    queued.repo,
                    queued.branch,
                    queued.checkpoint_commit,
                    base=config.pr_base,
                )
                self._copy_restart_artifacts(
                    queued.source_run_id,
                    run_dir,
                    config=config,
                    start_phase=queued.start_phase,
                    checkpoint=queued.checkpoint_commit,
                )
            elif not config_workspace.requested_branch_exists:
                workspace = self._workspaces.prepare(
                    queued.repo, queued.branch, base=config.pr_base
                )
                # The configured base may differ from the remote default branch used for
                # discovery. Resolve the final config from the exact commit the new branch uses.
                if queued.mode != "review":
                    config = load_config(
                        workspace.path,
                        personas_root=self._personas_root,
                        runs_root=self._runs_root,
                        vllm_url=self._vllm_url,
                    ).select_workflow(queued.workflow)
            config = apply_model_overrides(config, queued.model_overrides)
            deps = self._deps_for(
                workspace.path, config, clear_prefix_cache=queued.clear_prefix_cache
            )
            controls.cancel_active = getattr(deps, "cancel_active", None)
            if config.project_board and queued.mode != "review":
                try:
                    ProjectBoard(SubprocessRunner(str(workspace.path))).move_issue(
                        queued.repo, queued.ticket, config.project_board, "In progress"
                    )
                except GitError:
                    pass
        except (ConfigError, GitError, UnknownRunnerError, WorkspaceError) as exc:
            self._finish(state, RunStatus.FAILED, str(exc))
            return

        state.branch = workspace.branch
        state.workflow = queued.workflow
        state.pr_number = state.pr_number or queued.pr_number
        state.pr_head_sha = queued.pr_head_sha or state.pr_head_sha
        state.feedback_digest = queued.feedback_digest or state.feedback_digest
        state.source_run_id = queued.source_run_id
        state.start_phase = queued.start_phase
        self._drive(state, queued, config, deps, workspace.path, run_dir)

    def _deps_for(
        self, directory: Path, config: QuillfolioConfig, *, clear_prefix_cache: bool = False
    ) -> PipelineDeps:
        """Pipeline dependencies bound to **this run's** checkout.

        Built per run, not once at startup: the runner CLI, git reader and build/test command all
        operate inside a specific repo, and the model-server backend is a per-repo config choice.
        """
        return PipelineDeps.with_runner(
            get_runner(config.runner, directory=str(directory)),
            loader=make_model_server(config, clear_prefix_cache=clear_prefix_cache),
            git=GitOps(run=SubprocessRunner(directory=str(directory))),
            build_test=build_test_runner(str(directory)),
        )

    def _drive(
        self,
        state: RunState,
        queued: QueuedRun,
        config: QuillfolioConfig,
        deps: PipelineDeps,
        directory: Path,
        run_dir: Path,
    ) -> None:
        controls = self._controls_for(queued.run_id)
        state.backend = config.backend  # drives token-cost pricing (local vs hosted)
        recorder = (
            CheckpointRecorder(
                directory,
                run_dir,
                run_id=queued.run_id,
                repo=queued.repo,
                branch=queued.branch,
                base_branch=config.pr_base,
                phases=tuple(config.phase_ids),
            )
            if queued.mode == "create"
            else None
        )

        def should_stop() -> bool:
            return controls.stop.is_set()

        def answer_decision(question: str) -> str | None:
            decision = _Decision(event=threading.Event())
            controls.decision = decision
            # Block this run until POST /decision answers (or a stop is requested).
            while not decision.event.wait(timeout=0.5):
                if controls.stop.is_set():
                    controls.decision = None
                    return None
            controls.decision = None
            return decision.answer

        with EventLog(run_dir) as event_log:
            live_lock = threading.Lock()
            inherited_phase_usage: dict[str, LiveUsage] = {}
            inherited_phase_tools: dict[str, dict[str, int]] = {}
            if queued.source_run_id is not None:
                inherited = build_breakdown(
                    queued.run_id,
                    run_dir,
                    {"status": "queued", "backend": state.backend},
                    usd_per_1m=self._usd_per_1m,
                )
                for execution in inherited.get("phase_executions", []):
                    if not isinstance(execution, dict) or not isinstance(
                        phase_id := execution.get("phase"), str
                    ):
                        continue
                    prior = inherited_phase_usage.get(phase_id, LiveUsage())
                    inherited_phase_usage[phase_id] = LiveUsage(
                        prior.input_tokens + int(execution.get("context_tokens") or 0),
                        prior.output_tokens + int(execution.get("output_tokens") or 0),
                        prior.context_window_tokens
                        + int(execution.get("context_window_tokens") or 0),
                    )
                    phase_tools = inherited_phase_tools.setdefault(phase_id, {})
                    raw_tools = execution.get("tool_calls_by_name")
                    if isinstance(raw_tools, dict):
                        for name, count in raw_tools.items():
                            if isinstance(name, str) and isinstance(count, int):
                                phase_tools[name] = phase_tools.get(name, 0) + count
            usage_attribution = _LiveUsageAttribution(inherited_phase_usage)
            live_phase_usage = usage_attribution.phase_usage
            active_execution_usage = usage_attribution.active_execution_usage
            live_phase_tools = {
                phase: dict(tools) for phase, tools in inherited_phase_tools.items()
            }
            live_tokens = LiveUsage(
                sum(item.input_tokens for item in live_phase_usage.values()),
                sum(item.output_tokens for item in live_phase_usage.values()),
                sum(item.context_window_tokens for item in live_phase_usage.values()),
            )
            live_tools: dict[str, int] = {}
            for phase_tools in live_phase_tools.values():
                for name, count in phase_tools.items():
                    live_tools[name] = live_tools.get(name, 0) + count

            def publish_live(event_type: str) -> None:
                with live_lock:
                    phase = state.phase or "unknown"
                    phase_tokens = live_phase_usage.get(phase, LiveUsage())
                    phase_tools = live_phase_tools.get(phase, {})
                    phase_total = phase_tokens.total_tokens
                    phase_ids = live_phase_usage.keys() | live_phase_tools.keys()
                    phase_usages = {}
                    for phase_id in phase_ids:
                        usage = live_phase_usage.get(phase_id, LiveUsage())
                        tools = live_phase_tools.get(phase_id, {})
                        phase_usages[phase_id] = {
                            "context_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "total_tokens": usage.total_tokens,
                            "context_window_tokens": usage.context_window_tokens,
                            "cost": token_cost(
                                usage.total_tokens,
                                0.0,
                                backend=state.backend,
                                usd_per_1m=self._usd_per_1m,
                            ),
                            "tools": dict(tools),
                            "tool_calls_total": sum(tools.values()),
                        }
                    active_phase_usages = {}
                    for phase_id, usage in active_execution_usage.items():
                        tools = live_phase_tools.get(phase_id, {})
                        active_phase_usages[phase_id] = {
                            "context_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "total_tokens": usage.total_tokens,
                            "context_window_tokens": usage.context_window_tokens,
                            "cost": token_cost(
                                usage.total_tokens,
                                0.0,
                                backend=state.backend,
                                usd_per_1m=self._usd_per_1m,
                            ),
                            "tools": dict(tools),
                            "tool_calls_total": sum(tools.values()),
                        }
                    window_usage = phase_window_usage(list(phase_usages.values()))
                    window_total = int(window_usage["total_tokens"])
                    payload: dict[str, object] = {
                        "context_tokens": int(window_usage["context_tokens"]),
                        "output_tokens": int(window_usage["output_tokens"]),
                        "total_tokens": window_total,
                        "context_window_tokens": window_total,
                        "cost": token_cost(
                            window_total,
                            0.0,
                            backend=state.backend,
                            usd_per_1m=self._usd_per_1m,
                        ),
                        "tools": dict(live_tools),
                        "tool_calls_total": sum(live_tools.values()),
                        "phase": phase,
                        "phase_usages": phase_usages,
                        "active_phase_usages": active_phase_usages,
                        "phase_usage": {
                            "context_tokens": phase_tokens.input_tokens,
                            "output_tokens": phase_tokens.output_tokens,
                            "total_tokens": phase_total,
                            "context_window_tokens": phase_tokens.context_window_tokens,
                            "cost": token_cost(
                                phase_total,
                                0.0,
                                backend=state.backend,
                                usd_per_1m=self._usd_per_1m,
                            ),
                            "tools": dict(phase_tools),
                            "tool_calls_total": sum(phase_tools.values()),
                        },
                    }
                    state.live_usage = payload
                message: Event = {"type": event_type, "usage": payload}
                if self._live_publish is not None:
                    self._live_publish(message, state)
                else:
                    self._bus.publish_threadsafe({**message, "run_id": state.run_id})

            def on_event(event: Event) -> None:
                event_phase = event.get("phase")
                if event.get("type") == events.PHASE_STARTED and isinstance(event_phase, str):
                    with live_lock:
                        usage_attribution.start_execution(event_phase)
                if event.get("type") == events.RUN_FAILED:
                    raw_reason = event.get("reason")
                    raw_phase = event.get("phase")
                    reason = raw_reason if isinstance(raw_reason, str) else None
                    phase = raw_phase if isinstance(raw_phase, str) else state.phase
                    model_server_healthy = True
                    if config.backend == "vllm":
                        with VllmServer(config.vllm_url) as vllm:
                            model_server_healthy = vllm.healthy()
                    failure = classify_terminal_failure(
                        reason,
                        phase,
                        backend=config.backend,
                        model_server_healthy=model_server_healthy,
                    )
                    event = {**event, "failure_code": failure.code, "failure_label": failure.label}
                # Persist first: subscribers and in-memory state must never get ahead of the
                # crash-safe record used to explain this run later.
                event_log.append(event)
                state.fold_event(event)
                if self._live_publish is not None:
                    self._live_publish(event, state)
                else:
                    self._bus.publish_threadsafe({**event, "run_id": queued.run_id})
                if (
                    event.get("type") == events.PHASE_DONE
                    and event.get("phase") in {"commit", "commit_update"}
                    and event.get("verdict") in {"DONE", "PASS"}
                    and config.project_board
                ):
                    try:
                        ProjectBoard(SubprocessRunner(str(directory))).move_issue(
                            queued.repo, queued.ticket, config.project_board, "In review"
                        )
                    except GitError:
                        pass

            def on_tool_progress(phase: str, tally: dict[str, int], stream_path: Path) -> None:
                # Fires on every tool call during a phase — the only signal while an agent grinds
                # for minutes. Merge its tally into the current live payload without persisting a
                # high-frequency state transition. Pi usage arrives independently on every model
                # stream update; other runners retain transcript-derived compatibility behavior.
                nonlocal live_tokens, live_tools
                with live_lock:
                    baseline = inherited_phase_tools.get(phase, {})
                    live_phase_tools[phase] = {
                        name: baseline.get(name, 0) + count for name, count in tally.items()
                    }
                    for name, count in baseline.items():
                        live_phase_tools[phase].setdefault(name, count)
                    live_tools = {}
                    for phase_tools in live_phase_tools.values():
                        for name, count in phase_tools.items():
                            live_tools[name] = live_tools.get(name, 0) + count
                    if state.backend != "vllm":
                        usage = cumulative_live_usage(stream_path.parent)
                        live_tokens = LiveUsage(
                            int(usage.get("input_tokens", 0)),
                            int(usage.get("output_tokens", 0)),
                            int(usage.get("context_window_tokens", 0)),
                        )
                publish_live("tool_progress")

            deps.on_tool_progress = on_tool_progress

            def on_usage_progress(phase: str, snapshot: LiveUsage, _stream_path: Path) -> None:
                nonlocal live_tokens
                with live_lock:
                    usage_attribution.update(phase, snapshot)
                    live_tokens = LiveUsage(
                        sum(item.input_tokens for item in live_phase_usage.values()),
                        sum(item.output_tokens for item in live_phase_usage.values()),
                        sum(item.context_window_tokens for item in live_phase_usage.values()),
                    )
                publish_live("usage_progress")

            deps.on_usage_progress = on_usage_progress

            try:
                self._run_pipeline(
                    queued.ticket,
                    directory=str(directory),
                    run_id=queued.run_id,
                    mode=queued.mode,
                    workflow=queued.workflow,
                    clear_prefix_cache=queued.clear_prefix_cache,
                    config=config,
                    run_dir=run_dir,
                    deps=deps,
                    on_event=on_event,
                    should_stop=should_stop,
                    answer_decision=answer_decision,
                    start_phase=queued.start_phase,
                    checkpoint_phase=recorder.before_phase if recorder is not None else None,
                )
            except Exception as exc:  # noqa: BLE001 - never let a driver crash kill the worker
                reason = f"internal error: {exc}"
                on_event(events.run_failed(reason=reason, phase=state.phase))
            finally:
                deps.loader.unload_all()

        preserve = False
        if recorder is not None and state.status in {RunStatus.FAILED, RunStatus.HALTED}:
            try:
                preserve = recorder.recover_terminal(state.phase)
            except (GitError, OSError):
                preserve = False
        self._cleanup_terminal_workspace(state, preserve=preserve)
        self._record_history(state)
        self._notify_terminal(state)
        self._controls.pop(queued.run_id, None)

    # -- helpers ------------------------------------------------------------------

    def _controls_for(self, run_id: str) -> _RunControls:
        with self._lock:
            return self._controls.setdefault(run_id, _RunControls())

    def _finish(self, state: RunState, status: RunStatus, reason: str) -> None:
        """Terminate a run that never reached (or fell out of) the pipeline."""
        failure = classify_failure(reason, state.phase)
        event = (
            events.run_failed(
                reason=reason,
                phase=state.phase,
                failure_code=failure.code,
                failure_label=failure.label,
            )
            if status is RunStatus.FAILED
            else events.run_halted(reason=reason, phase=state.phase)
        )
        with EventLog(self._runs_root / state.run_id) as event_log:
            event_log.append(event)
        state.fold_event(event)
        self._cleanup_terminal_workspace(state)
        if self._live_publish is not None:
            self._live_publish(event, state)
        else:
            self._bus.publish_threadsafe({**event, "run_id": state.run_id})
        self._record_history(state)
        self._notify_terminal(state)
        self._controls.pop(state.run_id, None)

    def _notify_terminal(self, state: RunState) -> None:
        """Notify automation after persistence without changing the run's own result."""
        if self._on_terminal is None:
            return
        try:
            self._on_terminal(state)
        except Exception:  # noqa: BLE001,S110 - follow-up automation cannot rewrite this result
            pass

    def _cleanup_terminal_workspace(self, state: RunState, *, preserve: bool = False) -> None:
        """Discard and remove a failed/halted run branch without masking its original result."""
        if (
            preserve
            or state.source_run_id is not None
            or state.status not in (RunStatus.FAILED, RunStatus.HALTED)
            or state.started_at is None
            or not state.branch
            or state.mode in {"update", "review"}
        ):
            return
        try:
            self._workspaces.discard_run_branch(state.repo, state.branch, base="main")
        except WorkspaceError as exc:
            cleanup_error = f"workspace cleanup failed: {exc}"
            state.error = f"{state.error}; {cleanup_error}" if state.error else cleanup_error

    def _copy_restart_artifacts(
        self,
        source_run_id: str | None,
        target: Path,
        *,
        config: QuillfolioConfig,
        start_phase: str | None,
        checkpoint: str | None,
    ) -> None:
        """Validate and seed only the selected boundary's contract/evidence closure."""
        if source_run_id is None:
            raise WorkspaceError("restart source run is missing")
        if start_phase is None or checkpoint is None:
            raise WorkspaceError("restart boundary is missing its phase or checkpoint identity")
        source = self._runs_root / source_run_id
        if not source.is_dir():
            raise WorkspaceError(f"restart source artifacts for {source_run_id} are missing")
        try:
            prepare_contract_restart(
                source,
                target,
                config=config,
                start_phase=start_phase,
                source_run_id=source_run_id,
                checkpoint=checkpoint,
            )
        except RestartError as exc:
            raise WorkspaceError(f"restart contract closure is invalid: {exc}") from exc

    def _record_history(self, state: RunState) -> None:
        if self._history_record is not None:
            self._history_record(state)
        if self._breakdown_record is not None:
            data: dict[str, object] = {
                "status": state.status.value,
                "repo": state.repo,
                "branch": state.branch,
                "ticket": state.ticket,
                "mode": state.mode,
                "workflow": state.workflow,
                "pr_number": state.pr_number,
                "pr_head_sha": state.pr_head_sha,
                "feedback_digest": state.feedback_digest,
                "source_run_id": state.source_run_id,
                "start_phase": state.start_phase,
                "backend": state.backend,
                "clear_prefix_cache": state.clear_prefix_cache,
                "queued_at": state.queued_at,
                "started_at": state.started_at,
                "updated_at": state.updated_at,
                "pr_url": state.pr_url,
                "error": state.error,
                "failure_code": state.failure_code,
                "failure_label": state.failure_label,
                "history": [asdict(entry) for entry in state.history],
            }
            breakdown = build_breakdown(
                state.run_id, self._runs_root / state.run_id, data, usd_per_1m=self._usd_per_1m
            )
            self._breakdown_record(state.run_id, breakdown, SCHEMA_VERSION)
