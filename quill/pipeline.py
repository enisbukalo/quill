"""Pipeline entry point — load config, set up the run, drive the engine (ticket #33).

``run_pipeline`` is the single seam the bare CLI and the FastAPI service share. It is now thin:
it loads the repo's ``quillvault/quillfolio.toml``, creates the per-run directory
``quillvault/<run-id>/``, builds a :class:`~quill.runctx.RunContext`, and hands the configured
phase list to :func:`quill.engine.run_phases`. All phase logic lives in the data-driven engine;
there are no hardcoded phases here.

The three callbacks keep it UI-agnostic:

  on_event(event)            fired on every phase transition / retry / gate verdict
  should_stop() -> bool      polled between phases; True => halt cleanly (API stop)
  answer_decision(q) -> str  resolve a needs-decision live instead of halting
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from quill import engine
from quill.config import QuillfolioConfig, load_config
from quill.events import Event
from quill.mechanical import build_test_runner
from quill.runctx import (
    MODE_CREATE,
    AnswerDecision,
    BuildTest,
    OnEvent,
    PipelineDeps,
    RunContext,
    ShouldStop,
    PhaseCheckpointHook,
)
from quill.restart import SEED_NAME, restart_contract_refs

# Re-exported so the CLI/API import these from one place (their historical home).
__all__ = [
    "BuildTest",
    "PipelineDeps",
    "build_test_runner",
    "make_run_id",
    "run_dir_for",
    "run_pipeline",
]


def _noop_event(_: Event) -> None: ...
def _never_stop() -> bool:
    return False


def _no_answer(_: str) -> str | None:
    return None


def make_run_id(ticket: int, *, now: datetime | None = None) -> str:
    """Run id ``<timestamp>-ticket<N>`` (sortable, ties to the ticket) — #33 decision 8."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-ticket{ticket}"


def run_dir_for(config: QuillfolioConfig, run_id: str) -> Path:
    """``<repo>/quillvault/<run-id>/`` — this run's artifact/findings/log directory."""
    return config.runs_root / run_id


def run_pipeline(
    ticket: int,
    directory: str | None = None,
    start_phase: str | None = None,
    *,
    run_id: str | None = None,
    mode: str = MODE_CREATE,
    workflow: str | None = None,
    clear_prefix_cache: bool = False,
    config: QuillfolioConfig | None = None,
    run_dir: Path | None = None,
    deps: PipelineDeps,
    on_event: OnEvent = _noop_event,
    should_stop: ShouldStop = _never_stop,
    answer_decision: AnswerDecision = _no_answer,
    checkpoint_phase: PhaseCheckpointHook | None = None,
) -> Event:
    """Run the configured phase pipeline for a ticket. Returns a final summary event.

    ``directory`` is the target repo (defaults to cwd). ``start_phase`` is a configured phase
    **id** (not an int) to resume from. ``run_id`` lets the caller pin the per-run dir name
    (e.g. resuming); otherwise a fresh timestamped id is minted. ``mode`` is ``"create"`` (ship the
    ticket) or ``"update"`` (revise the ticket's open PR from its review feedback); the same phase
    list runs either way — update mode only changes what the phases are primed with.

    ``config`` and ``run_dir`` let a caller that has already resolved them pass them straight in.
    The service does this after preparing its checkout; its artifacts belong in server state rather
    than under the repo it is about to push.
    """
    directory = directory or "."
    config = config if config is not None else load_config(directory)
    selected_workflow = workflow or config.workflow_id
    config = config.select_workflow(selected_workflow)

    run_id = run_id or make_run_id(ticket)
    run_dir = run_dir if run_dir is not None else run_dir_for(config, run_id)
    _ensure_dir(run_dir)

    ctx = RunContext(
        config=config,
        deps=deps,
        ticket=ticket,
        run_id=run_id,
        run_dir=run_dir,
        on_event=on_event,
        should_stop=should_stop,
        answer_decision=answer_decision,
        directory=Path(directory).resolve(),
        mode=mode,
        workflow=selected_workflow,
        clear_prefix_cache=clear_prefix_cache,
        checkpoint_phase=checkpoint_phase,
    )
    if start_phase is not None and (run_dir / SEED_NAME).is_file():
        ctx.contracts.update(
            restart_contract_refs(run_dir, config=config, start_phase=start_phase)
        )
    return engine.run_phases(ctx, start_phase=start_phase)


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # best-effort; an agent write failure surfaces as GARBAGE downstream
