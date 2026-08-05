"""RunState + the in-memory store (WI-9, extended for the multi-repo service).

The API holds the authoritative live state. The driver only emits events; the API folds them into
RunState. High-level only — no raw logs.

Runs are keyed by id rather than held in a single "active" slot. Execution is still serialised (the
GPU is exclusive), but a queue means several runs exist at once — one running, the rest waiting —
and each needs its own state, stop flag, and pending decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, cast

from quill import events
from quill.phase_graph import PhaseGraph
from quill.runctx import MODE_CREATE


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_DECISION = "needs_decision"
    HALTED = "halted"
    DONE = "done"
    FAILED = "failed"


#: Statuses that still occupy the pipeline: the run is unfinished and its state may still change.
ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.NEEDS_DECISION)
#: Statuses a run never leaves.
TERMINAL_STATUSES = (RunStatus.HALTED, RunStatus.DONE, RunStatus.FAILED)


@dataclass(slots=True)
class PhaseEntry:
    phase: str  # "0".."6", "4a"/"4b"/"4c"
    label: str
    verdict: str | None  # terminal Outcome value; None only for legacy/incomplete history
    attempt: int
    ts: float = field(default_factory=time.time)
    phase_type: str | None = None
    model: str | None = None
    duration_s: float | None = None
    tools: dict[str, int] = field(default_factory=dict)
    reason: str | None = None
    contract_kind: str | None = None
    contract_version: int | None = None
    contract_status: str | None = None
    contract_digest: str | None = None


@dataclass(slots=True)
class ModelLoadEntry:
    load_id: str
    phase: str
    label: str
    model: str
    started_at: float
    duration_s: float | None = None
    status: Literal["active", "completed", "failed"] = "active"
    reason: str | None = None


@dataclass(slots=True)
class RunState:
    run_id: str
    ticket: int
    #: The repo and branch this run ships. Required now that one service serves many repos — a run
    #: id alone no longer says what is being worked on.
    repo: str = ""
    branch: str | None = None
    #: "create" (ship the ticket) or "update" (revise its open PR from review feedback).
    mode: str = MODE_CREATE
    workflow: str = "ticket"
    pr_number: int | None = None
    pr_head_sha: str | None = None
    feedback_digest: str | None = None
    source_run_id: str | None = None
    start_phase: str | None = None
    #: The model-server backend this run used (``llamacpp``/``vllm``/…). Drives token cost pricing.
    backend: str = ""
    clear_prefix_cache: bool = False
    #: Latest live usage snapshot pushed during a phase (tokens + cost + tools), for the frontend.
    #: Ephemeral — never persisted; refreshed on every tool call while a phase executes.
    live_usage: dict[str, object] = field(default_factory=dict)
    status: RunStatus = RunStatus.QUEUED
    phase: str | None = None  # current phase, incl sub-phase 4a/4b/4c
    phase_label: str | None = None
    phase_started_at: float | None = None
    #: Configured phase lanes currently executing. Usually one; concurrent audits expose several.
    active_phases: dict[str, float] = field(default_factory=dict)
    phase_type: str | None = None
    model: str | None = None
    #: Internal work inside the configured phase, e.g. model loading versus agent execution.
    activity: str = "queued"
    activity_label: str = "Queued"
    attempt: int = 0
    max_attempts: int = 0
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None  # None while still queued
    updated_at: float = field(default_factory=time.time)
    pr_url: str | None = None
    question: str | None = None  # set on needs_decision
    #: Why it failed or halted, so a client that missed the event stream can still say what broke.
    error: str | None = None
    failure_code: str | None = None
    failure_label: str | None = None
    history: list[PhaseEntry] = field(default_factory=list)
    #: Actual backend model switches. Already-resident checks are intentionally absent.
    model_loads: list[ModelLoadEntry] = field(default_factory=list)
    phase_graph: PhaseGraph | None = None
    #: Same-session completion audit state for configured phases.
    self_checks: dict[str, str] = field(default_factory=dict)
    #: Same-session malformed-output recovery state, populated only when recovery runs.
    self_fixes: dict[str, str] = field(default_factory=dict)
    #: Latest high-level contract lifecycle per phase; payloads remain artifact-endpoint only.
    contract_states: dict[str, dict[str, object]] = field(default_factory=dict)
    #: Every configured phase entry, including retry-loop re-entry, in execution order.
    phase_sequence: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def touch(self) -> None:
        self.updated_at = time.time()

    def mark_started(self) -> None:
        self.status = RunStatus.RUNNING
        self.started_at = self.started_at or time.time()
        self.touch()

    def fold_event(self, event: events.Event) -> None:
        """Apply one driver event to this authoritative state.

        The driver only emits events (see :mod:`quill.events`); the API folds each into
        the live RunState. Unknown event types are ignored so a newer driver can't crash
        an older API.
        """
        etype = event.get("type")

        if etype == events.RUN_STARTED:
            self.mark_started()
            self.activity = "starting"
            self.activity_label = "Preparing run"
            self.clear_prefix_cache = event.get("clear_prefix_cache") is True
            # Keep the canonical owner/name accepted by POST /runs. Pipeline config may carry a
            # display URL here, which must not break later repository filtering/discovery.
            if not self.repo:
                self.repo = _as_str(event.get("repo")) or self.repo
            self.workflow = _as_str(event.get("workflow")) or self.workflow
            pr_number = event.get("pr_number")
            if isinstance(pr_number, int):
                self.pr_number = pr_number
            self.pr_head_sha = _as_str(event.get("pr_head_sha")) or self.pr_head_sha
            self.feedback_digest = _as_str(event.get("feedback_digest")) or self.feedback_digest

        elif etype == events.RUN_PLAN:
            self.phase_graph = _as_phase_graph(event.get("phase_graph"))

        elif etype == events.PHASE_STARTED:
            self.status = RunStatus.RUNNING
            self.phase = _as_str(event.get("phase"))
            self.phase_label = _as_str(event.get("label"))
            self.phase_started_at = _as_float(event.get("ts")) or time.time()
            if self.phase is not None:
                self.active_phases[self.phase] = self.phase_started_at
            self.phase_type = _as_str(event.get("phase_type"))
            self.model = _as_str(event.get("model"))
            self.attempt = _as_int(event.get("attempt"), default=1)
            self.max_attempts = _as_int(event.get("max_attempts"), default=1)
            self.question = None
            self.activity = "executing_phase"
            phase_label = self.phase_label or self.phase
            self.activity_label = f"Executing {phase_label}" if phase_label else "Executing phase"
            if self.phase is not None:
                self.phase_sequence.append(self.phase)

        elif etype == events.MODEL_LOADING:
            phase = _as_str(event.get("phase"))
            model = _as_str(event.get("model"))
            self.phase = phase or self.phase
            self.phase_label = _as_str(event.get("label")) or self.phase_label
            self.model = model or self.model
            if phase is not None:
                self.active_phases.pop(phase, None)
            self.phase_started_at = None
            self.model_loads.append(
                ModelLoadEntry(
                    load_id=f"model-load-{len(self.model_loads) + 1}",
                    phase=phase or self.phase or "",
                    label=_as_str(event.get("label")) or self.phase_label or phase or "",
                    model=model or "",
                    started_at=_as_float(event.get("ts")),
                )
            )
            self.activity = "loading_model"
            self.activity_label = f"Loading model {model}" if model else "Loading model"

        elif etype == events.MODEL_LOAD_DONE:
            phase = _as_str(event.get("phase"))
            model = _as_str(event.get("model"))
            for load in reversed(self.model_loads):
                if load.status == "active" and load.phase == phase and load.model == (model or ""):
                    load.duration_s = _as_optional_float(event.get("duration_s"))
                    load.status = "completed" if event.get("success") is True else "failed"
                    load.reason = _as_str(event.get("reason"))
                    break

        elif etype == events.PHASE_EXECUTING:
            phase = _as_str(event.get("phase")) or self.phase
            started_at = _as_float(event.get("ts"))
            self.phase = phase
            self.phase_started_at = started_at
            if phase is not None:
                self.active_phases[phase] = started_at
            self.activity = "executing_phase"
            phase_label = _as_str(event.get("label")) or self.phase_label
            self.activity_label = f"Executing {phase_label}" if phase_label else "Executing phase"

        elif etype == events.SELF_CHECK_STARTED:
            phase = _as_str(event.get("phase")) or self.phase
            if phase is not None:
                self.self_checks[phase] = "active"
            self.activity = "self_check"
            self.activity_label = f"Self-checking {self.phase_label or phase or 'phase'}"

        elif etype == events.SELF_CHECK_DONE:
            phase = _as_str(event.get("phase")) or self.phase
            if phase is not None:
                verdict = (_as_str(event.get("verdict")) or "failed").lower()
                self.self_checks[phase] = "passed" if verdict in {"done", "pass"} else "failed"

        elif etype == events.SELF_FIX_STARTED:
            phase = _as_str(event.get("phase")) or self.phase
            if phase is not None:
                self.self_fixes[phase] = "active"
            self.activity = "self_fix"
            self.activity_label = f"Self-fixing {self.phase_label or phase or 'phase'}"

        elif etype == events.SELF_FIX_DONE:
            phase = _as_str(event.get("phase")) or self.phase
            if phase is not None:
                self.self_fixes[phase] = "completed" if event.get("repaired") is True else "failed"

        elif etype in {
            events.PROJECTION_STARTED,
            events.PROJECTION_DONE,
            events.CONTRACT_VALIDATED,
            events.CONTRACT_INCOMPLETE,
            events.CONTRACT_PUBLISHED,
        }:
            phase = _as_str(event.get("phase")) or self.phase
            if phase is not None:
                current = dict(self.contract_states.get(phase, {}))
                current["phase"] = phase
                current["kind"] = _as_str(event.get("contract_kind")) or current.get("kind", "")
                if etype == events.PROJECTION_STARTED:
                    current["state"] = "projecting"
                    current["attempt"] = _as_int(event.get("attempt"), default=self.attempt)
                    self.activity = "projecting_contract"
                    self.activity_label = f"Projecting {self.phase_label or phase} handoff"
                elif etype == events.PROJECTION_DONE:
                    current["state"] = "projected" if event.get("valid") is True else "rejected"
                elif etype == events.CONTRACT_INCOMPLETE:
                    current["state"] = "incomplete"
                    current["missing_count"] = _as_int(event.get("missing_count"), default=0)
                elif etype == events.CONTRACT_VALIDATED:
                    current["state"] = "validated"
                    current["status"] = _as_str(event.get("contract_status")) or ""
                else:
                    current["state"] = "published"
                    current["version"] = _as_int(event.get("contract_version"), default=0)
                    current["status"] = _as_str(event.get("contract_status")) or ""
                    current["digest"] = _as_str(event.get("contract_digest")) or ""
                    current["attempt"] = _as_int(event.get("attempt"), default=self.attempt)
                self.contract_states[phase] = current

        elif etype == events.PHASE_DONE or etype == events.GATE_VERDICT:
            self._record(event, verdict=_as_str(event.get("verdict")))
            completed_phase = _as_str(event.get("phase"))
            if completed_phase is not None:
                self.active_phases.pop(completed_phase, None)

        elif etype == events.RETRY:
            self.attempt = _as_int(event.get("attempt"), default=self.attempt)
            self.max_attempts = _as_int(event.get("max_attempts"), default=self.max_attempts)

        elif etype == events.NEEDS_DECISION:
            self.status = RunStatus.NEEDS_DECISION
            self.question = _as_str(event.get("question"))
            self.activity = "waiting_decision"
            self.activity_label = "Waiting for operator decision"

        elif etype == events.RUN_HALTED:
            self.status = RunStatus.HALTED
            self.error = _as_str(event.get("reason"))
            self.activity = "halted"
            self.activity_label = "Run halted"
            self.failure_code = _as_str(event.get("failure_code")) or "user_halted"
            self.failure_label = _as_str(event.get("failure_label")) or "Run halted by operator"

        elif etype == events.RUN_DONE:
            self.status = RunStatus.DONE
            self.pr_url = _as_str(event.get("pr_url")) or self.pr_url
            self.activity = "done"
            self.activity_label = "Run completed"

        elif etype == events.RUN_FAILED:
            self.status = RunStatus.FAILED
            self.error = _as_str(event.get("reason"))
            self.activity = "failed"
            self.activity_label = "Run failed"
            self.failure_code = _as_str(event.get("failure_code"))
            self.failure_label = _as_str(event.get("failure_label"))

        self.touch()

    def _record(self, event: events.Event, *, verdict: str | None) -> None:
        """Append a compact history entry from the event's phase/label (if present)."""
        phase = _as_str(event.get("phase")) or self.phase
        if phase is None:
            return
        self.history.append(
            PhaseEntry(
                phase=phase,
                label=_as_str(event.get("label")) or self.phase_label or "",
                verdict=verdict,
                attempt=self.attempt,
                ts=_as_float(event.get("ts")),
                phase_type=_as_str(event.get("phase_type")) or self.phase_type,
                model=_as_str(event.get("model")) or self.model,
                duration_s=_as_optional_float(event.get("duration_s")),
                tools=_as_tool_counts(event.get("tools")),
                reason=_as_str(event.get("reason")),
                contract_kind=_as_str(event.get("contract_kind")),
                contract_version=_as_optional_int(event.get("contract_version")),
                contract_status=_as_str(event.get("contract_status")),
                contract_digest=_as_str(event.get("contract_digest")),
            )
        )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: object) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else time.time()


def _as_optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_tool_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: count
        for key, count in value.items()
        if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool)
    }


def _as_phase_graph(value: object) -> PhaseGraph | None:
    """Validate the small persisted graph contract without trusting arbitrary JSON shapes."""
    if not isinstance(value, dict):
        return None
    raw_nodes = value.get("nodes")
    raw_edges = value.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return None
    nodes: list[dict[str, object]] = []
    for node in raw_nodes:
        if not isinstance(node, dict):
            return None
        node_id = node.get("id")
        label = node.get("label")
        phase_type = node.get("type")
        order = node.get("order")
        if not all(isinstance(item, str) for item in (node_id, label, phase_type)):
            return None
        if not isinstance(order, int) or isinstance(order, bool):
            return None
        column = node.get("column")
        lane = node.get("lane")
        group = node.get("group")
        self_check = node.get("self_check", False)
        self_fix = node.get("self_fix", False)
        if column is not None and (not isinstance(column, int) or isinstance(column, bool)):
            return None
        if lane is not None and (not isinstance(lane, int) or isinstance(lane, bool)):
            return None
        if group is not None and not isinstance(group, str):
            return None
        if not isinstance(self_check, bool):
            return None
        if not isinstance(self_fix, bool):
            return None
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "type": phase_type,
                "order": order,
                "column": column,
                "lane": lane if lane is not None else 0,
                "group": group,
                "self_check": self_check,
                "self_fix": self_fix,
            }
        )
    edges: list[dict[str, object]] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            return None
        key, source, target, kinds = (
            edge.get("key"),
            edge.get("source"),
            edge.get("target"),
            edge.get("kinds"),
        )
        if not all(isinstance(item, str) for item in (key, source, target)):
            return None
        if not isinstance(kinds, list) or any(kind not in ("normal", "retry") for kind in kinds):
            return None
        contracts = edge.get("contracts", [])
        if not isinstance(contracts, list) or any(not isinstance(item, str) for item in contracts):
            return None
        edges.append(
            {"key": key, "source": source, "target": target, "kinds": kinds, "contracts": contracts}
        )
    # Every field was narrowed above; spelling the final typed objects keeps this validator strict.
    typed_nodes = [
        {
            "id": str(node["id"]),
            "label": str(node["label"]),
            "type": str(node["type"]),
            "order": cast(int, node["order"]),
            "column": cast(int | None, node["column"]),
            "lane": cast(int, node["lane"]),
            "group": cast(str | None, node["group"]),
            "self_check": cast(bool, node["self_check"]),
            "self_fix": cast(bool, node["self_fix"]),
        }
        for node in nodes
    ]
    typed_edges = [
        {
            "key": str(edge["key"]),
            "source": str(edge["source"]),
            "target": str(edge["target"]),
            "kinds": [kind for kind in edge["kinds"] if kind in ("normal", "retry")],
            "contracts": [str(item) for item in cast(list[object], edge["contracts"])],
        }
        for edge in edges
        if isinstance(edge["kinds"], list)
    ]
    return cast(PhaseGraph, {"nodes": typed_nodes, "edges": typed_edges})


class RunStore:
    """Every run this process knows about, keyed by id.

    Bounded on purpose: finished runs are trimmed once SQLite has their summary, so an always-on
    service does not accumulate state forever. The DB is the durable record; this is the live view.
    """

    #: Terminal runs kept in memory for immediate re-query before falling back to the DB.
    MAX_FINISHED = 50

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}

    def add(self, run: RunState) -> None:
        self._runs[run.run_id] = run
        self._trim()

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def all(self) -> list[RunState]:
        return sorted(self._runs.values(), key=lambda r: r.queued_at)

    def discard(self, run_id: str) -> None:
        """Forget a terminal run after its durable history has been deleted."""
        run = self._runs.get(run_id)
        if run is not None and not run.is_active:
            self._runs.pop(run_id, None)

    @property
    def active(self) -> RunState | None:
        """The run currently executing, if any. Only ever one — the GPU is exclusive."""
        for run in self.all():
            if run.status in (RunStatus.RUNNING, RunStatus.NEEDS_DECISION):
                return run
        return None

    def queued(self) -> list[RunState]:
        """Runs waiting to start, oldest first."""
        return [r for r in self.all() if r.status is RunStatus.QUEUED]

    def _trim(self) -> None:
        finished = [r for r in self.all() if not r.is_active]
        for run in finished[: max(0, len(finished) - self.MAX_FINISHED)]:
            self._runs.pop(run.run_id, None)
