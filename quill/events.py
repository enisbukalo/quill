"""Pipeline event vocabulary (WI-9).

The driver stays UI-agnostic: its only seam to the outside world is the `on_event`
callback, which it calls with a small **JSON-serializable dict** at every transition. This
module defines that vocabulary — the event `type` constants and typed factory functions —
so the driver and the API agree on the shape without the driver importing anything web.

Every event carries a `type` and a `ts` (epoch seconds); the rest is per-event payload.
Keep payloads high-level — phase/verdict/attempt, never raw logs.
"""

from __future__ import annotations

import time
from typing import Literal

from quill.phase_graph import PhaseGraph

Event = dict[str, object]

# Event type tags. Listed in the order a successful run emits them.
RUN_QUEUED = "run_queued"
RUN_STARTED = "run_started"
RUN_PLAN = "run_plan"
PHASE_STARTED = "phase_started"
MODEL_LOADING = "model_loading"
MODEL_LOAD_DONE = "model_load_done"
PHASE_EXECUTING = "phase_executing"
SELF_CHECK_STARTED = "self_check_started"
SELF_CHECK_DONE = "self_check_done"
SELF_FIX_STARTED = "self_fix_started"
SELF_FIX_DONE = "self_fix_done"
PHASE_DONE = "phase_done"
GATE_VERDICT = "gate_verdict"
RETRY = "retry"
NEEDS_DECISION = "needs_decision"
RUN_HALTED = "run_halted"
RUN_DONE = "run_done"
RUN_FAILED = "run_failed"

Verdict = Literal["PASS", "BLOCK"]


def _event(type_: str, **payload: object) -> Event:
    """Build an event dict with `type` + `ts`, dropping None-valued payload keys."""
    event: Event = {"type": type_, "ts": time.time()}
    event.update({k: v for k, v in payload.items() if v is not None})
    return event


def run_queued(
    run_id: str,
    ticket: int,
    *,
    repo: str,
    branch: str,
    mode: str,
    workflow: str = "ticket",
    model_overrides: dict[str, str] | None = None,
    source_run_id: str | None = None,
    start_phase: str | None = None,
) -> Event:
    return _event(
        RUN_QUEUED,
        run_id=run_id,
        ticket=ticket,
        repo=repo,
        branch=branch,
        mode=mode,
        workflow=workflow,
        model_overrides=model_overrides,
        source_run_id=source_run_id,
        start_phase=start_phase,
    )


def run_started(
    run_id: str,
    ticket: int,
    *,
    repo: str | None = None,
    title: str | None = None,
    clear_prefix_cache: bool = False,
    workflow: str | None = None,
    pr_number: int | None = None,
    pr_head_sha: str | None = None,
    feedback_digest: str | None = None,
) -> Event:
    return _event(
        RUN_STARTED,
        run_id=run_id,
        ticket=ticket,
        repo=repo,
        title=title,
        clear_prefix_cache=clear_prefix_cache,
        workflow=workflow,
        pr_number=pr_number,
        pr_head_sha=pr_head_sha,
        feedback_digest=feedback_digest,
    )


def run_plan(
    summary: str, *, lines: list[str] | None = None, phase_graph: PhaseGraph | None = None
) -> Event:
    """The run's execution plan (runner, build cmds, ordered phases + models) as a preformatted
    block. Emitted right after ``run_started`` so the terminal and file log both show what the run
    intends to do before any phase runs. ``lines`` carries the same content structured, for API
    consumers that render their own layout."""
    return _event(RUN_PLAN, summary=summary, lines=lines, phase_graph=phase_graph)


def phase_started(
    phase: str,
    label: str,
    *,
    attempt: int = 1,
    max_attempts: int = 1,
    phase_type: str | None = None,
    model: str | None = None,
) -> Event:
    return _event(
        PHASE_STARTED,
        phase=phase,
        label=label,
        attempt=attempt,
        max_attempts=max_attempts,
        phase_type=phase_type,
        model=model,
    )


def model_loading(
    phase: str, label: str, model: str, *, session_capacity: int | None = None
) -> Event:
    """The configured phase is waiting for its implicit model preparation step."""
    return _event(
        MODEL_LOADING,
        phase=phase,
        label=label,
        model=model,
        session_capacity=session_capacity,
    )


def model_load_done(
    phase: str,
    label: str,
    model: str,
    *,
    duration_s: float,
    success: bool,
    reason: str | None = None,
) -> Event:
    """Finish one model-switch attempt with durable timing and outcome evidence."""
    return _event(
        MODEL_LOAD_DONE,
        phase=phase,
        label=label,
        model=model,
        duration_s=duration_s,
        success=success,
        reason=reason,
    )


def phase_executing(phase: str, label: str, *, model: str | None = None) -> Event:
    """Model preparation finished and the configured phase's worker is about to execute."""
    return _event(PHASE_EXECUTING, phase=phase, label=label, model=model)


def self_check_started(phase: str, label: str) -> Event:
    return _event(SELF_CHECK_STARTED, phase=phase, label=label)


def self_check_done(phase: str, label: str, *, verdict: str, duration_s: float) -> Event:
    return _event(SELF_CHECK_DONE, phase=phase, label=label, verdict=verdict, duration_s=duration_s)


def self_fix_started(phase: str, label: str) -> Event:
    return _event(SELF_FIX_STARTED, phase=phase, label=label)


def self_fix_done(phase: str, label: str, *, repaired: bool, duration_s: float) -> Event:
    return _event(
        SELF_FIX_DONE,
        phase=phase,
        label=label,
        repaired=repaired,
        duration_s=duration_s,
    )


def phase_done(
    phase: str,
    label: str,
    *,
    verdict: str | None = None,
    model: str | None = None,
    duration_s: float | None = None,
    tools: dict[str, int] | None = None,
    reason: str | None = None,
) -> Event:
    """``tools`` is the phase's tool-call tally (``{"edit": 24, "read": 31, ...}``), which the
    console renders as a breakdown and the file log keeps as the phase's permanent record. A phase
    that ran no tools (or a runner that reports none) omits it. ``reason`` explains nonstandard
    terminal outcomes such as CRASH or GARBAGE."""
    return _event(
        PHASE_DONE,
        phase=phase,
        label=label,
        verdict=verdict,
        model=model,
        duration_s=duration_s,
        tools=tools or None,
        reason=reason,
    )


def gate_verdict(
    phase: str,
    verdict: Verdict,
    *,
    label: str | None = None,
    model: str | None = None,
    duration_s: float | None = None,
    tools: dict[str, int] | None = None,
    reason: str | None = None,
) -> Event:
    """A gated phase reports its verdict here instead of via :func:`phase_done`, so it carries the
    same ``tools`` tally — else every reviewer/finalizer would silently lose its tool counts.

    ``reason`` is the judge's own one-line summary from its receipt (``BLOCK: 3 unmet MAJOR
    findings — ...``). Without it a BLOCK shows only a verdict, and the *why* lives solely in a
    findings file the reader has to go open."""
    return _event(
        GATE_VERDICT,
        phase=phase,
        verdict=verdict,
        label=label,
        model=model,
        duration_s=duration_s,
        tools=tools or None,
        reason=reason,
    )


def retry(phase: str, attempt: int, max_attempts: int, *, reason: str | None = None) -> Event:
    return _event(RETRY, phase=phase, attempt=attempt, max_attempts=max_attempts, reason=reason)


def needs_decision(question: str, *, phase: str | None = None) -> Event:
    return _event(NEEDS_DECISION, question=question, phase=phase)


def run_halted(*, reason: str | None = None, phase: str | None = None) -> Event:
    return _event(
        RUN_HALTED,
        reason=reason,
        phase=phase,
        failure_code="user_halted",
        failure_label="Run halted by operator",
    )


def run_done(*, pr_url: str | None = None) -> Event:
    return _event(RUN_DONE, pr_url=pr_url)


def run_failed(
    *,
    reason: str | None = None,
    phase: str | None = None,
    failure_code: str | None = None,
    failure_label: str | None = None,
) -> Event:
    return _event(
        RUN_FAILED,
        reason=reason,
        phase=phase,
        failure_code=failure_code,
        failure_label=failure_label,
    )
