"""Shared run context + injected dependencies for the data-driven engine (ticket #33).

The engine, the mechanical steps, and the producer/reviewer/finalizer phase runners all need
the same per-run state (config, ticket, run dir, callbacks) and the same injected collaborators
(model loader, spawn seam, git, build/test runner). Both live here so neither :mod:`quill.engine`
nor :mod:`quill.mechanical` has to import the other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import ClassVar, Protocol

from quill.config import QuillfolioConfig
from quill.contracts import ContractRef, ContractStatus
from quill.events import Event
from quill.git_ops import GitOps
from quill.live_usage import LiveUsage
from quill.phases import (
    ModelLoaderLike,
    PhaseResult,
    ReceiptExtractor,
    Spawner,
    extract_receipt,
)

#: Run modes. ``create`` ships a ticket, ``update`` revises an existing PR, and ``review`` audits
#: an existing PR without changing its branch.
MODE_CREATE = "create"
MODE_UPDATE = "update"
MODE_REVIEW = "review"
MODES = (MODE_CREATE, MODE_UPDATE, MODE_REVIEW)

OnEvent = Callable[[Event], None]
ShouldStop = Callable[[], bool]
AnswerDecision = Callable[[str], "str | None"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One observed local verification command, without parsing human console output."""

    command: str
    exit_code: int | None
    cancelled: bool
    timed_out: bool
    started_at: str
    ended_at: str
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.cancelled and not self.timed_out


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Typed result for an exact configured build/test selection."""

    selection: str
    commands: tuple[CommandResult, ...]

    @property
    def ok(self) -> bool:
        return bool(self.commands) and all(command.ok for command in self.commands)

    @property
    def combined_log(self) -> str:
        return "\n".join(f"$ {item.command}\n{item.output}" for item in self.commands)

    def __iter__(self) -> Iterator[bool | str]:
        """Preserve tuple unpacking for external runner callers during the typed seam migration."""
        yield self.ok
        yield self.combined_log

    def __getitem__(self, index: int) -> bool | str:
        return (self.ok, self.combined_log)[index]


@dataclass(frozen=True, slots=True)
class MechanicalEvidence:
    """Typed material retained only until the engine publishes a mechanical contract."""

    status: ContractStatus
    payload: object
    artifacts: tuple[str, ...] = ()


BuildTest = Callable[[QuillfolioConfig, str], "VerificationResult | tuple[bool, str]"]
#: Skill-load directive: (skill names) -> a prompt line in the runner's own trigger syntax ("" if none).
SkillDirective = Callable[[list[str]], str]
#: Live tool-call progress: the phase's tool tally so far ({"read": 20, "bash": 6}) -> None.
#: The whole tally is passed, not just the latest tool, so the display can show every count at once.
#: Live progress hook: called with the per-tool tally and the current phase's stream-transcript
#: path each time the agent starts a tool. The path lets a consumer read the phase's running token
#: usage straight from the live transcript (the API service prices it and pushes it to the browser).
ToolProgress = Callable[[str, dict[str, int], Path], None]
UsageProgress = Callable[[str, LiveUsage, Path], None]
CancelActive = Callable[[], None]
SessionRepair = Spawner
SessionCapacity = Callable[[str], int]
PhaseCheckpointHook = Callable[[str], str | None]


def _no_skill_directive(_names: list[str]) -> str:
    """Default when no runner is wired (tests): emit no skill-load line."""
    return ""


def _one_session(_model: str) -> int:
    """Safe capacity when no runner-specific discovery seam is wired."""
    return 1


def _safe_session_capacity(discover: SessionCapacity, model: str) -> int:
    """Normalize optional runner discovery without letting it break phase execution."""
    try:
        capacity = discover(model)
    except Exception:  # noqa: BLE001 - third-party runners may expose arbitrary discovery errors
        return 1
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        return 1
    return capacity


def _compose_cancel(*owners: object) -> CancelActive | None:
    """Return one best-effort cancellation hook for every process-owning dependency."""
    callbacks = tuple(
        callback for owner in owners if callable(callback := getattr(owner, "cancel", None))
    )
    if not callbacks:
        return None

    def cancel_all() -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001,S110 - one owner must not prevent the others stopping
                pass

    return cancel_all


class _RunnerLike(Protocol):
    """Structural view of :class:`quill.runners.Runner` — its pipeline seams."""

    def spawn(
        self,
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: Callable[[str], None] | None = None,
        on_usage: Callable[[LiveUsage], None] | None = None,
        abort_reason: Callable[[], str | None] | None = None,
    ) -> str: ...
    def extract_receipt(self, stdout: str) -> str | None: ...
    def skill_directive(self, names: list[str]) -> str: ...
    def available_session_capacity(self, model: str) -> int: ...
    def cancel(self) -> None: ...

    supports_session_repair: ClassVar[bool]

    def repair_session(
        self,
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: Callable[[str], None] | None = None,
        on_usage: Callable[[LiveUsage], None] | None = None,
        abort_reason: Callable[[], str | None] | None = None,
    ) -> str: ...


@dataclass(slots=True)
class PipelineDeps:
    """Injected collaborators so the engine is testable without real models/git."""

    loader: ModelLoaderLike
    spawn: Spawner
    git: GitOps | None = None
    build_test: BuildTest | None = None
    extract: ReceiptExtractor = extract_receipt
    skill_directive: SkillDirective = _no_skill_directive
    session_capacity: SessionCapacity = _one_session
    #: Live tool-call progress: called with (total-so-far, tool name) as each tool starts. The
    #: engine stays UI-agnostic (see quill.events) — the CLI injects the console's in-place
    #: counter here, the API service leaves it None and reads the totals off `phase_done`.
    on_tool_progress: ToolProgress | None = None
    on_usage_progress: UsageProgress | None = None
    cancel_active: CancelActive | None = None
    session_repair: SessionRepair | None = None

    @classmethod
    def with_runner(
        cls,
        runner: _RunnerLike,
        *,
        loader: ModelLoaderLike,
        git: GitOps | None = None,
        build_test: BuildTest | None = None,
        on_tool_progress: ToolProgress | None = None,
        on_usage_progress: UsageProgress | None = None,
    ) -> PipelineDeps:
        """Build deps from a :class:`~quill.runners.Runner`, wiring its seams."""
        repair = (
            getattr(runner, "repair_session", None)
            if getattr(runner, "supports_session_repair", False)
            else None
        )
        discover_capacity = getattr(runner, "available_session_capacity", _one_session)
        return cls(
            loader=loader,
            spawn=runner.spawn,
            git=git,
            build_test=build_test,
            extract=runner.extract_receipt,
            skill_directive=runner.skill_directive,
            session_capacity=lambda model: _safe_session_capacity(discover_capacity, model),
            on_tool_progress=on_tool_progress,
            on_usage_progress=on_usage_progress,
            cancel_active=_compose_cancel(runner, build_test),
            session_repair=repair,
        )


@dataclass(slots=True)
class RunContext:
    """Mutable per-run state threaded through every phase."""

    config: QuillfolioConfig
    deps: PipelineDeps
    ticket: int
    run_id: str
    run_dir: Path  # quillvault/<run-id>/ — where artifacts + findings + logs land
    on_event: OnEvent
    should_stop: ShouldStop
    answer_decision: AnswerDecision
    #: Target repository checkout. Used for deterministic Git-side mechanics around agent phases.
    directory: Path = Path(".")
    #: ``"create"`` (default) ships the ticket from scratch; ``"update"`` resumes an existing open
    #: PR — the run checks out that PR's branch and every phase is primed with its review feedback.
    mode: str = MODE_CREATE
    workflow: str = "ticket"
    clear_prefix_cache: bool = False
    branch: str | None = None
    title: str = ""
    body: str = ""
    pr_url: str | None = None
    #: Update mode: the open PR this run revises, and its flattened review feedback. Both are
    #: resolved once (see :func:`quill.engine._ensure_ticket`) and injected into every phase prompt.
    pr_number: int | None = None
    feedback: str = ""
    pr_head_sha: str = ""
    pr_head_committed_at: str = ""
    feedback_digest: str = ""
    feedback_ids: tuple[str, ...] = ()
    decisions: list[tuple[str, str]] = field(default_factory=list)
    feedback_threads: tuple[str, ...] = ()
    history: list[PhaseResult] = field(default_factory=list)
    #: Per-phase tool-call tally (``{"impl": {"edit": 24, "read": 31}}``), filled as each worker
    #: reports its tool calls and drained onto that phase's ``phase_done``. Per-run state, not
    #: module state: the API service runs concurrent runs in one process, and a shared tally would
    #: cross-report their counts.
    tool_tally: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Exact cumulative Pi usage per configured phase, including retries/spawns.
    phase_usage: dict[str, LiveUsage] = field(default_factory=dict)
    #: Latest logical Pi session contribution for each phase. A same-session continuation replaces
    #: this context-window contribution while its processed input/output remains cumulative.
    phase_session_usage: dict[str, LiveUsage] = field(default_factory=dict)
    #: Unique transcript sequence per phase/model so retries never overwrite earlier evidence.
    transcript_counts: dict[str, int] = field(default_factory=dict)
    #: Number of times each configured phase has actually been invoked, including revisions and
    #: verification passes. Emitted as the phase attempt number for ordered breakdowns.
    phase_call_counts: dict[str, int] = field(default_factory=dict)
    #: Revise rounds each gated phase has consumed across the whole run. A gate's loop lives inside
    #: ``_run_with_fresh_attempts``, so a CRASH/GARBAGE re-entry would otherwise hand the gate a
    #: fresh budget; this tally makes ``retry_budget`` a per-run ceiling instead of a per-attempt one.
    gate_rounds_spent: dict[str, int] = field(default_factory=dict)
    #: Latest validated durable contract for every configured phase/audit lane.
    contracts: dict[str, ContractRef] = field(default_factory=dict)
    contract_corrections: dict[str, str] = field(default_factory=dict)
    retry_contracts: dict[str, ContractRef] = field(default_factory=dict)
    mechanical_evidence: dict[str, MechanicalEvidence] = field(default_factory=dict)
    #: Model-switch wall time accumulated inside each active phase attempt. Terminal phase
    #: durations subtract it because model preparation is reported as its own timed operation.
    phase_model_load_s: dict[str, float] = field(default_factory=dict)
    #: The preset the last spawn asked the loader for — a **scheduling hint, not authoritative
    #: state**. Used only to order a reviewer fan-out so the already-resident model runs first
    #: (see :func:`quill.engine._affinity_order`). The loader re-checks the router before every
    #: load, so a stale value here costs at most one reload that would have happened anyway; it is
    #: cleared only when model preparation fails and leaves the router in an unknown state.
    loaded_preset: str | None = None
    #: Concurrent audit lanes serialize durable event callbacks through this lock.
    event_lock: RLock = field(default_factory=RLock, repr=False)
    #: Optional durable boundary captured immediately before each configured phase.
    checkpoint_phase: PhaseCheckpointHook | None = None
    #: Exact checkpoint commit returned for the latest attempt of each phase, when available.
    phase_checkpoints: dict[str, str] = field(default_factory=dict)

    def artifact_path(self, name: str) -> Path:
        """Absolute path to an artifact/findings file ``name`` in this run's dir."""
        return self.run_dir / name
