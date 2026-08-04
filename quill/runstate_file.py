"""Durable per-run state for resuming a halted run (ticket #33).

Each run writes its state to ``quillvault/<run-id>/state.json`` as it progresses, recording the
last phase reached (by **id**, not an int), why it stopped, and a fingerprint of the phase set it
ran under. A vault-level pointer ``quillvault/last-run.json`` records the most recent run id so
``--resume`` can find it without being told.

Because phase ids are user-defined, resume can't assume 0-6. On resume the CLI compares the saved
``phase_set_hash`` to the current config's :meth:`~quill.config.QuillfolioConfig.phase_set_hash`:
if they differ the config changed since that run and resuming would run a phase plan that no
longer lines up — so resume is refused (#33 decision 5).

Driven off the event stream: :func:`make_recorder` returns an ``on_event`` hook that updates the
file (and the pointer) on every transition.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from quill import events
from quill.config import QuillfolioConfig

STATE_FILENAME = "state.json"
LAST_RUN_FILENAME = "last-run.json"


@dataclass(slots=True)
class RunStateFile:
    """What we persist to resume a run."""

    ticket: int
    run_id: str
    phase: str | None = None  # last phase id reached
    status: str = "running"  # running / halted / needs_decision / failed / done
    question: str | None = None  # set when status == needs_decision
    branch: str | None = None
    phase_set_hash: str = ""  # config fingerprint this run executed under
    workflow: str = "ticket"
    clear_prefix_cache: bool = False


def state_path(config: QuillfolioConfig, run_id: str) -> Path:
    return config.runs_root / run_id / STATE_FILENAME


def last_run_path(config: QuillfolioConfig) -> Path:
    return config.runs_root / LAST_RUN_FILENAME


def write_state(path: Path, state: RunStateFile) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    except OSError:
        pass  # persistence is best-effort; a run still completes without it


def read_state(path: Path) -> RunStateFile | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "ticket" not in data or "run_id" not in data:
        return None
    return RunStateFile(
        ticket=int(data["ticket"]),
        run_id=str(data["run_id"]),
        phase=data.get("phase"),
        status=str(data.get("status", "running")),
        question=data.get("question"),
        branch=data.get("branch"),
        phase_set_hash=str(data.get("phase_set_hash", "")),
        workflow=str(data.get("workflow", "ticket")),
        clear_prefix_cache=data.get("clear_prefix_cache") is True,
    )


def _write_last_run(path: Path, run_id: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    except OSError:
        pass


def read_last_run_id(config: QuillfolioConfig) -> str | None:
    try:
        data = json.loads(last_run_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run_id = data.get("run_id") if isinstance(data, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


class ResumeError(RuntimeError):
    """Resume can't proceed: no saved run, wrong ticket, already done, or config changed."""


def resume_target(config: QuillfolioConfig, ticket: int) -> tuple[str, str, bool]:
    """Resolve ``(run_id, start_phase_id)`` for ``--resume``, or raise :class:`ResumeError`.

    Reads the latest run's state, checks it's for ``ticket``, isn't already done, and — crucially
    — that its ``phase_set_hash`` still matches the current config. A mismatch means the phase set
    changed since that run, so resuming is refused.
    """
    run_id = read_last_run_id(config)
    if run_id is None:
        raise ResumeError(
            f"no saved run state in {config.runs_root} — nothing to resume (ticket {ticket})."
        )
    state = read_state(state_path(config, run_id))
    if state is None:
        raise ResumeError(f"saved run '{run_id}' has no readable state — nothing to resume.")
    if state.ticket != ticket:
        raise ResumeError(f"saved run is for ticket {state.ticket}, not {ticket}.")
    if state.status == "done":
        raise ResumeError(f"ticket {ticket} already completed — nothing to resume.")
    try:
        selected = config.select_workflow(state.workflow)
    except Exception as exc:
        raise ResumeError(f"saved workflow '{state.workflow}' is no longer configured.") from exc
    if state.phase_set_hash and state.phase_set_hash != selected.phase_set_hash():
        raise ResumeError(
            "the phase config changed since this run — refusing to resume against a different "
            "phase plan. Start a fresh run instead."
        )
    if state.phase is None or selected.phase(state.phase) is None:
        raise ResumeError(
            f"saved run's phase '{state.phase}' is not in the current config — cannot resume."
        )
    return run_id, state.phase, state.clear_prefix_cache


def make_recorder(
    config: QuillfolioConfig,
    ticket: int,
    run_id: str,
    base_on_event: Callable[[dict[str, object]], None],
    *,
    clear_prefix_cache: bool = False,
) -> Callable[[dict[str, object]], None]:
    """Wrap an ``on_event`` so each transition updates this run's state file + the pointer.

    Stamps the run's ``phase_set_hash`` from ``config`` so a later ``--resume`` can verify the
    config hasn't changed.
    """
    path = state_path(config, run_id)
    pointer = last_run_path(config)
    state = RunStateFile(
        ticket=ticket,
        run_id=run_id,
        phase_set_hash=config.phase_set_hash(),
        workflow=config.workflow_id,
        clear_prefix_cache=clear_prefix_cache,
    )
    _write_last_run(pointer, run_id)

    def on_event(event: dict[str, object]) -> None:
        base_on_event(event)
        etype = event.get("type")
        if etype == events.PHASE_STARTED:
            phase = event.get("phase")
            state.phase = phase if isinstance(phase, str) else state.phase
            state.status = "running"
            state.question = None
        elif etype == events.NEEDS_DECISION:
            state.status = "needs_decision"
            q = event.get("question")
            state.question = q if isinstance(q, str) else None
        elif etype == events.RUN_HALTED:
            state.status = "halted"
        elif etype == events.RUN_FAILED:
            state.status = "failed"
        elif etype == events.RUN_DONE:
            state.status = "done"
        write_state(path, state)

    return on_event
