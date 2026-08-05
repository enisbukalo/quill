"""Data-driven phase executor (ticket #33).

Replaces the hardcoded ``_phaseN_*`` wiring. :func:`run_phases` walks the configured phase list
and dispatches each entry on its ``type`` — running the same loop for every LLM phase (load model
→ assemble prompt → spawn → classify receipt → gate/retry) and handing mechanical phases to their
built-in step. Adding, removing, reordering, or renaming a phase is config + ``.md`` only; nothing
about the flow is special-cased here per phase.

Flow control is declarative, read off each :class:`~quill.config.PhaseDef`:

* ``gates`` — a BLOCK triggers the revise→verify retry loop (else the verdict is informational).
* ``retry_budget`` — how many revise→verify rounds.
* ``on_block`` — which phase(s) to re-run, in order, on a BLOCK.
* ``reconciles`` — a finalizer's input phases; the engine resolves each to its findings files and
  injects the paths.

Reviewer fan-out: a ``reviewer`` phase with ``models = [a, b, ...]`` runs its persona once per
model (sequentially — one model loaded at a time), each writing ``review-<id>-<slug>.md``. A
downstream finalizer reconciles them.

Concurrent audits: a vLLM ``reviewer`` with nested ``audits`` loads their shared model once and
runs every named, independently observable Pi session in parallel. The finalizer reconciles their
stable per-audit findings files exactly like sequential fan-out output.

Concurrent producers: consecutive ``producer`` phases with one ``parallel_group`` load their
shared vLLM model once and run as independent Pi sessions. Each lane keeps a canonical latest
artifact plus immutable attempt snapshots for downstream synthesis and selective gate retries.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from quill import events
from quill.attribution import commit_attribution
from quill.blocker_memory import capture_blocker, resolve_blocker, verified_memory_block
from quill.config import AuditDef, PhaseDef, QuillfolioConfig, slugify
from quill.events import Event
from quill.findings import (
    Finding,
    deterministic_gate_result,
    deterministic_review_result,
    load_findings,
    materialize_verification_delta,
    merge_verification_findings,
)
from quill.live_usage import LiveUsage
from quill.mechanical import (
    PR_REVIEW_NAME,
    _load_pr_review,
    _title_from,
    body_text_from,
    run_mechanical,
)
from quill.personas import load_persona, load_persona_body
from quill.phase_graph import build_phase_graph
from quill.phases import (
    GateResult,
    Outcome,
    PhaseResult,
    SpawnError,
    SpawnTimeout,
    classify_receipt,
    run_gate,
    run_preloaded_phase,
)
from quill.runctx import MODE_REVIEW, MODE_UPDATE, RunContext

_STRUCTURED_REPAIR_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class GateRound:
    """Where one gate evaluation sits inside its revise→verify loop.

    Carried by every structured-gate resolution so :class:`~quill.findings.BlockingPolicy` can make
    late discovery advisory without losing the ability to block on a repeated defect.
    """

    #: 0 for the initial review; 1-based for each revise round.
    index: int = 0
    #: Blocker IDs the gate held *before* this round ran. Findings written during the round (a
    #: re-audited lane's fresh output, merged into the verification contract) are deliberately not
    #: in here — otherwise every new finding would trivially qualify as a repeat.
    carried_ids: frozenset[str] = frozenset()
    #: Whether this is the last round the gate's budget allows.
    final: bool = False


def run_phases(ctx: RunContext, *, start_phase: str | None = None) -> Event:
    """Execute the configured phase list; return the final summary event.

    ``start_phase`` (a phase id) resumes from that phase, skipping earlier ones. The loop polls
    ``ctx.should_stop`` before each phase and parks on a NEEDS_DECISION the answerer can't resolve.
    """
    # Fetch the ticket first so run_started can carry its title (and we fail before announcing a
    # run we can't actually drive).
    if not _ensure_ticket(ctx):
        ctx.on_event(
            events.run_started(
                run_id=ctx.run_id,
                ticket=ctx.ticket,
                repo=ctx.config.repo,
                clear_prefix_cache=ctx.clear_prefix_cache,
                workflow=ctx.workflow,
            )
        )
        return _fail(ctx, reason=f"ticket #{ctx.ticket} has no fetchable body", phase="ticket")

    # Update mode needs an open PR to revise. Resolve it (and its review feedback) before
    # announcing the run: without a PR there is no branch to check out and no feedback to act on,
    # so the run would silently degrade into a second from-scratch attempt on the same ticket.
    target_error = _ensure_pr_target(ctx)
    if target_error is not None:
        ctx.on_event(
            events.run_started(
                run_id=ctx.run_id,
                ticket=ctx.ticket,
                repo=ctx.config.repo,
                clear_prefix_cache=ctx.clear_prefix_cache,
                workflow=ctx.workflow,
            )
        )
        return _fail(ctx, reason=target_error, phase="ticket")

    ctx.on_event(
        events.run_started(
            run_id=ctx.run_id,
            ticket=ctx.ticket,
            repo=ctx.config.repo,
            title=ctx.title or None,
            clear_prefix_cache=ctx.clear_prefix_cache,
            workflow=ctx.workflow,
            pr_number=ctx.pr_number,
            pr_head_sha=ctx.pr_head_sha or None,
            feedback_digest=ctx.feedback_digest or None,
        )
    )

    summary, plan_lines = _run_plan_summary(ctx, start_phase=start_phase)
    ctx.on_event(
        events.run_plan(summary, lines=plan_lines, phase_graph=build_phase_graph(ctx.config.phases))
    )

    started = start_phase is None
    phase_index = 0
    while phase_index < len(ctx.config.phases):
        phase = ctx.config.phases[phase_index]
        group = _parallel_members(ctx.config, phase)
        if not started:
            if start_phase in {member.id for member in group}:
                started = True
            else:
                phase_index += len(group)
                continue

        if ctx.should_stop():
            return _halt(ctx, reason="stop requested", phase=phase.id)

        executions = (
            _run_parallel_producers(ctx, group)
            if len(group) > 1
            else [(phase, _run_phase(ctx, phase))]
        )
        for executed, result in executions:
            ctx.history.append(result)

            if ctx.should_stop():
                return _halt(ctx, reason="stop requested", phase=executed.id)

            if result.needs_decision:
                ctx.on_event(events.needs_decision(result.question or "", phase=executed.id))
                if ctx.answer_decision(result.question or "") is None:
                    return _halt(ctx, reason="needs decision", phase=executed.id)
                return run_phases(ctx, start_phase=executed.id)

            if result.outcome in (Outcome.CRASH, Outcome.GARBAGE, Outcome.FAILED, Outcome.BLOCK):
                return _fail(ctx, reason=result.message, phase=executed.id)
        phase_index += len(group)

    done = events.run_done(pr_url=ctx.pr_url)
    ctx.on_event(done)
    return done


# -- run plan summary -------------------------------------------------------------


def _phase_models(phase: PhaseDef) -> str:
    """Human-readable model(s) for a phase, or an empty string for a model-less mechanical step."""
    if phase.audits:
        return f"{len(phase.audits)} concurrent audits → {phase.audits[0].model}"
    if phase.is_fanout:
        return " + ".join(phase.models)
    return phase.model or ""


def _parallel_members(config: QuillfolioConfig, phase: PhaseDef) -> list[PhaseDef]:
    """The consecutive producer stage containing ``phase``, or ``[phase]``."""
    if phase.parallel_group is None:
        return [phase]
    start = config.phase_ids.index(phase.id)
    while start > 0 and config.phases[start - 1].parallel_group == phase.parallel_group:
        start -= 1
    members: list[PhaseDef] = []
    for candidate in config.phases[start:]:
        if candidate.parallel_group != phase.parallel_group:
            break
        members.append(candidate)
    return members


def _phase_gate_note(config: QuillfolioConfig, phase: PhaseDef) -> str:
    """Trailing note describing a phase's gate/retry/on-block behavior, if any."""
    if not phase.gates:
        return ""
    note = f"gates (retry {config.retry_budget(phase)})"
    if phase.on_block:
        note += f", on BLOCK → {' → '.join(phase.on_block)}"
    return note


def _run_plan_summary(ctx: RunContext, *, start_phase: str | None) -> tuple[str, list[str]]:
    """Build the run's execution-plan block: runner + build/test commands + ordered phases with
    their type, model(s), and gate behavior. Returns (multi-line string, per-phase line list).

    The lines mirror the config so a reader sees, before anything runs, exactly what the run will
    attempt. ``start_phase`` (resume) marks earlier phases as skipped rather than hiding them.
    """
    cfg = ctx.config
    header = [
        "run plan",
        f"  ticket : #{ctx.ticket} {ctx.title or ''}".rstrip(),
        f"  repo   : {cfg.repo}  (PR base {cfg.pr_base})",
        f"  runner : {cfg.runner}",
        f"  build  : {cfg.build_command}",
        f"  test   : {cfg.test_command}",
        f"  phases : {len(cfg.phases)}",
    ]
    reached_start = start_phase is None
    rows: list[str] = []
    for i, phase in enumerate(cfg.phases, start=1):
        if start_phase in {member.id for member in _parallel_members(cfg, phase)}:
            reached_start = True
        skipped = not reached_start
        model = _phase_models(phase)
        gate = _phase_gate_note(cfg, phase)
        bits = [f"{i}.", f"{phase.id}", f"({phase.type})"]
        if model:
            bits.append(f"→ {model}")
        if phase.parallel_group:
            bits.append(f"[parallel {phase.parallel_group}]")
        if gate:
            bits.append(f"[{gate}]")
        if skipped:
            bits.append("(skipped: resume)")
        rows.append("  " + " ".join(bits))
    body = header + rows
    return "\n".join(body), rows


# -- per-type dispatch ------------------------------------------------------------


def _run_phase(
    ctx: RunContext, phase: PhaseDef, *, revise_findings: tuple[Finding, ...] = ()
) -> PhaseResult:
    """Run one configured phase, retrying a failed spawn/contract after bounded self-fix.

    ``revise_findings`` is set only when this phase is being re-run inside a gate's revise route;
    reviewers then re-audit in verification mode against those carried findings instead of deriving
    a fresh, unbounded set (see :func:`_gate`).
    """
    if ctx.checkpoint_phase is not None:
        try:
            ctx.checkpoint_phase(phase.id)
        except Exception as exc:  # noqa: BLE001 - Git boundary failures must fail safely
            return PhaseResult(Outcome.CRASH, f"could not save phase checkpoint: {exc}")

    return _run_with_fresh_attempts(
        ctx, phase, lambda: _dispatch_phase(ctx, phase, revise_findings=revise_findings)
    )


def _run_with_fresh_attempts(
    ctx: RunContext, phase: PhaseDef, attempt_phase: Callable[[], PhaseResult]
) -> PhaseResult:
    """Retry one phase operation after its own same-session recovery is exhausted."""
    result = attempt_phase()
    budget = ctx.config.spawn_retries()
    for attempt in range(1, budget + 1):
        if (
            result.outcome not in (Outcome.CRASH, Outcome.GARBAGE)
            or not result.allow_phase_retry
            or ctx.should_stop()
        ):
            return result
        checkpoint_error = _checkpoint_phases(ctx, [phase])
        if checkpoint_error is not None:
            return checkpoint_error
        _emit_event(
            ctx,
            events.retry(
                phase.id,
                attempt,
                budget,
                reason=f"fresh phase attempt after {result.outcome.value}: {result.message}",
            ),
        )
        result = attempt_phase()
    if result.outcome in (Outcome.CRASH, Outcome.GARBAGE):
        result.allow_phase_retry = False
    return result


def _dispatch_phase(
    ctx: RunContext, phase: PhaseDef, *, revise_findings: tuple[Finding, ...] = ()
) -> PhaseResult:
    """Run one fresh attempt of ``phase`` without taking another checkpoint."""
    if phase.type == "producer":
        return _run_producer(ctx, phase)
    if phase.type == "reviewer":
        return _run_reviewer(ctx, phase, revise_findings=revise_findings)
    if phase.type == "finalizer":
        return _run_finalizer(ctx, phase)
    if phase.type == "mechanical":
        return _run_mechanical(ctx, phase)
    # Unreachable: config validation rejects unknown types.
    return PhaseResult(Outcome.CRASH, f"unknown phase type {phase.type!r}")


# -- model affinity ---------------------------------------------------------------
#
# Loading a preset costs a full model swap (~40s on the llama.cpp router). `ModelLoader.load`
# already no-ops when the target is the resident preset, so a phase that repeats the previous
# phase's model is free. What is NOT free is the ORDER of a reviewer fan-out: `models = [a, b, c]`
# runs all three whatever the order, so running the already-resident one first and the one the next
# phase needs last saves up to two swaps at zero cost to the result. Reordering is safe because
# every per-model output is named from the model itself (`_findings_name`), never from its index.


def _affinity_order(
    models: tuple[str, ...], loaded: str | None, next_model: str | None
) -> list[str]:
    """``models`` with ``loaded`` moved to the front and ``next_model`` to the back.

    Order-only: membership and multiplicity are preserved, so the same passes run and write the
    same files either way. When one model is both the resident and the next phase's, the front
    wins — starting warm is certain, whereas the lookahead is a guess a gate can invalidate.
    """
    order = list(models)
    if next_model is not None and next_model in order:
        # Move the LAST occurrence: with a duplicate, the trailing one is already closest to the
        # end, so this is a no-op instead of a pointless shuffle.
        order.append(order.pop(len(order) - 1 - order[::-1].index(next_model)))
    if loaded is not None and loaded in order:
        order.insert(0, order.pop(order.index(loaded)))
    return order


def _next_phase_model(config: QuillfolioConfig, phase_id: str) -> str | None:
    """The first model any phase *after* ``phase_id`` will load, or ``None``.

    Model-less phases (mechanical steps) are skipped rather than treated as the end of the run:
    they load nothing, so the next preset the router is asked for is the one belonging to the next
    LLM phase beyond them.
    """
    ids = config.phase_ids
    if phase_id not in ids:
        return None
    for phase in config.phases[ids.index(phase_id) + 1 :]:
        if phase.models:
            return phase.models[0]
    return None


# -- event enrichment (model / type / timing) -------------------------------------


def _model_label(phase: PhaseDef) -> str | None:
    """The model(s) a phase runs on, for the console. Fan-out shows all, joined by ``+``."""
    if not phase.models:
        return None  # mechanical steps (branch/build_test) have no model
    return "+".join(phase.models)


def _emit_event(ctx: RunContext, event: Event) -> None:
    """Serialize event persistence/folding when concurrent audit threads report progress."""
    with ctx.event_lock:
        ctx.on_event(event)


def _emit_started(ctx: RunContext, phase: PhaseDef) -> float:
    """Emit phase_started (tagged with type + model) and return a monotonic start time."""
    with ctx.event_lock:
        call_number = ctx.phase_call_counts.get(phase.id, 0) + 1
        ctx.phase_call_counts[phase.id] = call_number
        ctx.phase_model_load_s[phase.id] = 0.0
        ctx.on_event(
            events.phase_started(
                phase.id,
                phase.label or phase.id,
                attempt=call_number,
                phase_type=phase.type,
                model=_model_label(phase),
            )
        )
    return time.monotonic()


def _phase_execution_duration(ctx: RunContext, phase: PhaseDef, started: float) -> float:
    """Return phase wall time without model preparation, which is timed independently."""
    elapsed = time.monotonic() - started
    model_load_s = ctx.phase_model_load_s.pop(phase.id, 0.0)
    return round(max(0.0, elapsed - model_load_s), 2)


def _emit_done(ctx: RunContext, phase: PhaseDef, *, verdict: str | None, started: float) -> None:
    """Emit phase_done carrying the model, elapsed wall time since ``started``, and tool tally."""
    _emit_event(
        ctx,
        events.phase_done(
            phase.id,
            phase.label or phase.id,
            verdict=verdict,
            model=_model_label(phase),
            duration_s=_phase_execution_duration(ctx, phase, started),
            tools=_take_tools(ctx, phase.id),
        ),
    )


def _emit_verdict_or_done(
    ctx: RunContext, phase: PhaseDef, result: PhaseResult, *, started: float
) -> None:
    """A gating phase emits gate_verdict on PASS/BLOCK, else a plain phase_done.

    Both carry the model + elapsed time so the console shows them for gated phases too.

    A BLOCK also carries the judge's own reason, so the line says *why* it blocked instead of
    sending the reader to the findings file to find out. Only on BLOCK: a PASS reason is a
    restatement of "it passed" and would bury the phases that actually need attention.
    """
    label = phase.label or phase.id
    model = _model_label(phase)
    duration = _phase_execution_duration(ctx, phase, started)
    tools = _take_tools(ctx, phase.id)
    if result.outcome in (Outcome.PASS, Outcome.BLOCK):
        _emit_event(
            ctx,
            events.gate_verdict(
                phase.id,
                "PASS" if result.is_pass else "BLOCK",
                label=label,
                model=model,
                duration_s=duration,
                tools=tools,
                reason=result.message if result.is_block else None,
            ),
        )
    else:
        _emit_event(
            ctx,
            events.phase_done(
                phase.id,
                label,
                verdict=_verdict_of(result),
                model=model,
                duration_s=duration,
                tools=tools,
                reason=result.message,
            ),
        )


def _run_producer(ctx: RunContext, phase: PhaseDef, *, findings: str | None = None) -> PhaseResult:
    """LLM writes an artifact; non-gating. Emits started + done.

    ``findings`` (a revise pass) points the producer at the reviewer's findings file so it fixes
    what was flagged instead of re-writing blind.
    """
    started = _emit_started(ctx, phase)
    result = _spawn_producer(ctx, phase, findings=findings)
    _emit_done(ctx, phase, verdict=_verdict_of(result), started=started)
    return result


def _checkpoint_phases(ctx: RunContext, phases: list[PhaseDef]) -> PhaseResult | None:
    """Capture serial Git boundaries before concurrent read-only producer lanes start."""
    if ctx.checkpoint_phase is None:
        return None
    try:
        for phase in phases:
            ctx.checkpoint_phase(phase.id)
    except Exception as exc:  # noqa: BLE001 - Git boundary failures must fail safely
        return PhaseResult(Outcome.CRASH, f"could not save phase checkpoint: {exc}")
    return None


def _model_needs_load(ctx: RunContext, model: str) -> bool:
    """Check the real backend when possible, falling back to this run's affinity hint."""
    checker = getattr(ctx.deps.loader, "needs_load", None)
    if callable(checker):
        try:
            return bool(checker(model))
        except Exception:  # noqa: BLE001 - the subsequent load reports the backend failure
            return True
    return ctx.loaded_preset != model


def _prepare_model(
    ctx: RunContext,
    phase: PhaseDef,
    model: str,
    *,
    session_capacity: int | None = None,
    inside_phase_attempt: bool,
) -> PhaseResult | None:
    """Load ``model`` and emit a timed operation only when the backend requires a switch."""
    label = phase.label or phase.id
    track_load = _model_needs_load(ctx, model)
    if track_load:
        _emit_event(
            ctx,
            events.model_loading(
                phase.id,
                label,
                model,
                session_capacity=session_capacity,
            ),
        )
    started = time.monotonic()
    try:
        ctx.deps.loader.load(model, ctx.config.model_load_seconds)
    except Exception as exc:  # noqa: BLE001 - model backends expose different failure types
        duration = round(time.monotonic() - started, 2)
        if track_load:
            _emit_event(
                ctx,
                events.model_load_done(
                    phase.id,
                    label,
                    model,
                    duration_s=duration,
                    success=False,
                    reason=str(exc),
                ),
            )
            if inside_phase_attempt:
                ctx.phase_model_load_s[phase.id] = (
                    ctx.phase_model_load_s.get(phase.id, 0.0) + duration
                )
        ctx.loaded_preset = None
        return PhaseResult(Outcome.CRASH, message=f"model load failed: {exc}")

    duration = round(time.monotonic() - started, 2)
    if track_load:
        _emit_event(
            ctx,
            events.model_load_done(
                phase.id,
                label,
                model,
                duration_s=duration,
                success=True,
            ),
        )
        if inside_phase_attempt:
            ctx.phase_model_load_s[phase.id] = ctx.phase_model_load_s.get(phase.id, 0.0) + duration
    ctx.loaded_preset = model
    return None


def _run_parallel_producers(
    ctx: RunContext,
    phases: list[PhaseDef],
    *,
    findings: str | None = None,
) -> list[tuple[PhaseDef, PhaseResult]]:
    """Run one configured producer group concurrently and preserve each latest lane artifact."""
    checkpoint_error = _checkpoint_phases(ctx, phases)
    if checkpoint_error is not None:
        return [(phase, checkpoint_error) for phase in phases]

    model = phases[0].model or ""
    available = max(1, ctx.deps.session_capacity(model))
    worker_count = min(len(phases), available)
    first = phases[0]
    failure = _prepare_model(
        ctx,
        first,
        model,
        session_capacity=worker_count,
        inside_phase_attempt=False,
    )
    if failure is not None:
        results: list[tuple[PhaseDef, PhaseResult]] = []
        for phase in phases:
            started = _emit_started(ctx, phase)
            _emit_done(ctx, phase, verdict="CRASH", started=started)
            results.append((phase, failure))
        return results

    def run_lane(phase: PhaseDef) -> PhaseResult:
        def attempt() -> PhaseResult:
            if ctx.should_stop():
                return PhaseResult(Outcome.CRASH, "stop requested before producer started")
            started = _emit_started(ctx, phase)
            artifact = phase.artifact or f"{phase.id}.md"
            result = _spawn_preloaded_llm(
                ctx,
                phase,
                model=model,
                task=_producer_task(
                    ctx,
                    phase,
                    artifact,
                    findings=findings,
                    finding_owner=phase.id if findings else None,
                ),
            )
            repaired = _repair_artifact_failure(
                ctx,
                phase,
                model=model,
                artifact=artifact,
                result=result,
            )
            _emit_done(ctx, phase, verdict=_verdict_of(repaired), started=started)
            if repaired.outcome in (Outcome.DONE, Outcome.PASS):
                _snapshot_parallel_artifact(ctx, phase)
            return repaired

        return _run_with_fresh_attempts(ctx, phase, attempt)

    completed: dict[str, PhaseResult] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=f"quill-{first.parallel_group or first.id}",
    ) as pool:
        futures = {pool.submit(run_lane, phase): phase for phase in phases}
        for future in as_completed(futures):
            phase = futures[future]
            completed[phase.id] = future.result()
    return [(phase, completed[phase.id]) for phase in phases]


def _snapshot_parallel_artifact(ctx: RunContext, phase: PhaseDef) -> None:
    """Keep immutable lane attempts while the canonical artifact remains the synthesis input."""
    if phase.artifact is None or phase.parallel_group is None:
        return
    with ctx.event_lock:
        source = ctx.artifact_path(phase.artifact)
        if not source.is_file():
            return
        attempt = ctx.phase_call_counts.get(phase.id, 1)
        suffix = source.suffix or ".md"
        snapshot_name = f"{phase.id}-attempt-{attempt}{suffix}"
        snapshot = ctx.artifact_path(snapshot_name)
        snapshot.write_bytes(source.read_bytes())
        manifest_path = ctx.artifact_path(f"parallel-{phase.parallel_group}-manifest.json")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "group": phase.parallel_group,
            "lanes": {},
        }
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, ValueError):
                pass
        raw_lanes = manifest.get("lanes")
        lanes = cast(dict[str, object], raw_lanes) if isinstance(raw_lanes, dict) else {}
        manifest["lanes"] = lanes
        lanes[phase.id] = {
            "attempt": attempt,
            "artifact": phase.artifact,
            "snapshot": snapshot_name,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _run_reviewer(
    ctx: RunContext, phase: PhaseDef, *, revise_findings: tuple[Finding, ...] = ()
) -> PhaseResult:
    """One-or-N reviewer passes.

    Fan-out (``len(models) > 1``): run the persona once per model, each writing its own findings
    file; the phase is informational input to a finalizer, so it returns DONE once all passes
    succeed. Single model: if the phase ``gates``, run the revise→verify gate; else it's an
    informational single-pass review (DONE).
    """
    if phase.audits:
        return _run_concurrent_audits(ctx, phase, revise_findings=revise_findings)
    if phase.is_fanout:
        started = _emit_started(ctx, phase)
        for model in _affinity_order(
            phase.models, ctx.loaded_preset, _next_phase_model(ctx.config, phase.id)
        ):
            findings = _findings_name(phase, model)
            result = _spawn_llm(ctx, phase, model=model, task=_review_task(ctx, phase, findings))
            result = _repair_artifact_failure(
                ctx, phase, model=model, artifact=findings, result=result
            )
            if phase.structured_findings:
                result = _resolve_structured_review(
                    ctx,
                    phase,
                    model=model,
                    artifact=findings,
                    result=result,
                    namespace=slugify(model),
                )
            if result.outcome not in (Outcome.DONE, Outcome.PASS):
                _emit_done(ctx, phase, verdict=_verdict_of(result), started=started)
                return result
        _emit_done(ctx, phase, verdict="DONE", started=started)
        return PhaseResult(Outcome.DONE, f"{len(phase.models)} reviewer passes wrote findings")

    # Single-model reviewer.
    started = _emit_started(ctx, phase)
    findings = _findings_name(phase, phase.model or "")
    initial = _spawn_llm(ctx, phase, model=phase.model, task=_review_task(ctx, phase, findings))
    initial = _repair_artifact_failure(
        ctx, phase, model=phase.model, artifact=findings, result=initial
    )
    if phase.gates and phase.structured_findings:
        initial = _resolve_structured_gate(
            ctx, phase, model=phase.model, artifact=findings, result=initial
        )
    elif phase.structured_findings:
        initial = _resolve_structured_review(
            ctx,
            phase,
            model=phase.model,
            artifact=findings,
            result=initial,
            namespace=slugify(phase.model or phase.id),
        )
    _emit_verdict_or_done(ctx, phase, initial, started=started)

    if phase.gates:
        return _gate(ctx, phase, initial)
    return initial


def _audit_phase(parent: PhaseDef, audit: AuditDef) -> PhaseDef:
    """Materialize one configured audit lane as an independently observable reviewer."""
    return replace(
        parent,
        id=f"{parent.id}.{audit.id}",
        label=audit.label,
        persona=audit.persona,
        skills=audit.skills,
        models=(audit.model,),
        audits=(),
    )


def _run_concurrent_audits(
    ctx: RunContext, phase: PhaseDef, *, revise_findings: tuple[Finding, ...] = ()
) -> PhaseResult:
    """Prepare one vLLM model, then run named audit lanes within live model capacity."""
    lanes = [(_audit_phase(phase, audit), audit) for audit in phase.audits]
    model = phase.audits[0].model
    available = max(1, ctx.deps.session_capacity(model))
    worker_count = min(len(lanes), available)
    result = _prepare_model(
        ctx,
        phase,
        model,
        session_capacity=worker_count,
        inside_phase_attempt=False,
    )
    if result is not None:
        for lane, _audit in lanes:
            started = _emit_started(ctx, lane)
            _emit_done(ctx, lane, verdict="CRASH", started=started)
        return result

    def run_lane(lane: PhaseDef, audit: AuditDef) -> PhaseResult:
        if ctx.should_stop():
            return PhaseResult(Outcome.CRASH, "stop requested before audit started")
        started = _emit_started(ctx, lane)
        findings = _audit_findings_name(phase, audit)
        task = _review_task(
            ctx,
            lane,
            findings,
            verify=bool(revise_findings),
            prior_findings=revise_findings,
        )
        result = _spawn_preloaded_llm(ctx, lane, model=model, task=task)
        repaired = _repair_artifact_failure(
            ctx, lane, model=model, artifact=findings, result=result
        )
        if phase.structured_findings:
            repaired = _resolve_structured_review(
                ctx,
                lane,
                model=model,
                artifact=findings,
                result=repaired,
                namespace=audit.id,
            )
        # Complete the lane while it still owns the worker slot. This makes the terminal event
        # visible before the executor admits the next queued lane, rather than allowing its start
        # event to race ahead of this completion in the coordinator thread.
        _emit_done(ctx, lane, verdict=_verdict_of(repaired), started=started)
        return repaired

    results: dict[str, PhaseResult] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix=f"quill-{phase.id}"
    ) as pool:
        futures = {pool.submit(run_lane, lane, audit): lane for lane, audit in lanes}
        for future in as_completed(futures):
            lane = futures[future]
            results[lane.id] = future.result()

    for lane, _audit in lanes:
        result = results[lane.id]
        if result.outcome not in (Outcome.DONE, Outcome.PASS):
            return result
    return PhaseResult(Outcome.DONE, f"{len(lanes)} concurrent audits wrote findings")


def _run_finalizer(ctx: RunContext, phase: PhaseDef) -> PhaseResult:
    """Reconcile N reviewers' findings (paths injected) and gate."""
    started = _emit_started(ctx, phase)
    artifact = phase.artifact or f"{phase.id}.md"
    initial = _spawn_llm(ctx, phase, model=phase.model, task=_finalizer_task(ctx, phase))
    initial = _repair_artifact_failure(
        ctx, phase, model=phase.model, artifact=artifact, result=initial
    )
    initial = _repair_pr_review_contract(
        ctx, phase, model=phase.model, artifact=artifact, result=initial
    )
    if phase.gates and phase.structured_findings:
        try:
            reconciled = _load_reconciled_findings(ctx, phase)
        except ValueError as exc:
            initial = PhaseResult(Outcome.GARBAGE, str(exc))
        else:
            initial = _resolve_structured_gate(
                ctx,
                phase,
                model=phase.model,
                artifact=artifact,
                result=initial,
                prior=reconciled,
            )
    _emit_verdict_or_done(ctx, phase, initial, started=started)

    if phase.gates:
        return _gate(ctx, phase, initial)
    return initial


def _repair_pr_review_contract(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    model: str | None,
    artifact: str,
    result: PhaseResult,
) -> PhaseResult:
    """Validate ``pr-review.json`` while its finalizer session can still repair it once."""
    if artifact != PR_REVIEW_NAME or result.outcome in (
        Outcome.CRASH,
        Outcome.FAILED,
        Outcome.NEEDS_DECISION,
    ):
        return result

    path = ctx.artifact_path(artifact)
    try:
        checked = _require_artifact(
            ctx,
            PhaseResult(Outcome.DONE, raw_receipt=result.raw_receipt),
            artifact,
            max_chars=phase.max_artifact_chars,
        )
        if checked.outcome is Outcome.GARBAGE:
            raise ValueError(checked.message)
        review = _load_pr_review(path)
        _require_pr_review_reconciliation(ctx, phase, review)
        return PhaseResult(Outcome.DONE, "validated PR review artifact")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        rejection = str(exc)

    if ctx.deps.session_repair is None:
        return PhaseResult(Outcome.GARBAGE, f"invalid PR review artifact: {rejection}")

    instruction = (
        f"Quill rejected the PR review artifact at {path}: {rejection}. Re-read the reviewer "
        "evidence and repair the JSON in place. Reconcile severity and verdict together: retain "
        "only substantiated CRITICAL or MAJOR findings; use BLOCK when any such finding remains, "
        "or PASS with no blocking findings when none remain. Do not blindly flip the verdict or "
        "discard a finding merely to satisfy validation. Preserve every input blocking finding "
        "ID and all required finding fields, "
        "write valid JSON only, and then emit the original task's DONE receipt."
    )
    fix_started = _self_fix_started(ctx, phase)
    repaired = _repair_llm_session(ctx, phase, model=model, prompt=instruction)
    if repaired.outcome in (Outcome.CRASH, Outcome.NEEDS_DECISION):
        _self_fix_done(ctx, phase, fix_started, repaired=False)
        return repaired
    if repaired.outcome is Outcome.FAILED:
        _self_fix_done(ctx, phase, fix_started, repaired=False)
        return _self_fix_failure(rejection, repaired)
    try:
        checked = _require_artifact(
            ctx,
            PhaseResult(Outcome.DONE, raw_receipt=repaired.raw_receipt),
            artifact,
            max_chars=phase.max_artifact_chars,
        )
        if checked.outcome is Outcome.GARBAGE:
            raise ValueError(checked.message)
        review = _load_pr_review(path)
        _require_pr_review_reconciliation(ctx, phase, review)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _self_fix_done(ctx, phase, fix_started, repaired=False)
        return PhaseResult(Outcome.GARBAGE, f"invalid PR review artifact after repair: {exc}")
    _self_fix_done(ctx, phase, fix_started, repaired=True)
    return PhaseResult(Outcome.DONE, "validated repaired PR review artifact")


def _require_pr_review_reconciliation(
    ctx: RunContext, phase: PhaseDef, review: dict[str, object]
) -> None:
    """Reject a PR finalizer that silently omits a blocking structured audit finding."""
    prior = tuple(finding for finding in _load_reconciled_findings(ctx, phase) if finding.blocks)
    rows = review.get("findings")
    if not isinstance(rows, list):
        raise ValueError("pr-review.json findings must be a list")
    current = {
        (str(row.get("id")), str(row.get("severity"))) for row in rows if isinstance(row, dict)
    }
    missing = [finding.id for finding in prior if (finding.id, finding.severity) not in current]
    if missing:
        raise ValueError(f"PR review omitted blocking audit finding(s): {', '.join(missing)}")


def _run_mechanical(ctx: RunContext, phase: PhaseDef) -> PhaseResult:
    """Dispatch a mechanical phase; gate build_test (and any gated mechanical) on BLOCK."""
    started = _emit_started(ctx, phase)
    result = run_mechanical(ctx, phase, spawn=_spawn_producer)
    _emit_verdict_or_done(ctx, phase, result, started=started)

    if phase.gates and result.outcome in (Outcome.PASS, Outcome.BLOCK):
        return _gate(
            ctx, phase, result, verify=lambda _a: run_mechanical(ctx, phase, spawn=_spawn_producer)
        )
    return result


# -- the gate ---------------------------------------------------------------------


def _gate(
    ctx: RunContext,
    phase: PhaseDef,
    initial: PhaseResult,
    *,
    verify: Callable[[int], PhaseResult] | None = None,
) -> PhaseResult:
    """Revise-then-verify loop for a gated phase, re-running its ``on_block`` phases."""
    if initial.outcome not in (Outcome.PASS, Outcome.BLOCK):
        return initial

    # A gate's revise rounds are spent per RUN, not per phase attempt. `_dispatch_phase` (and so
    # this whole loop) sits inside `_run_with_fresh_attempts`, which re-enters the phase on
    # CRASH/GARBAGE — without a durable tally that re-entry would silently restore a full budget and
    # `retry_budget = 3` could run five or more rounds, as observed on ticket #19.
    configured = ctx.config.retry_budget(phase)
    spent = ctx.gate_rounds_spent.get(phase.id, 0)
    budget = max(0, configured - spent)
    route = _retry_route(ctx.config, phase)
    # The file the first phase on the retry route must read to know WHAT to fix.
    findings = _gate_findings_artifact(ctx, phase)
    prior_findings: tuple[Finding, ...] = ()
    if findings is not None and phase.structured_findings:
        try:
            prior_findings = load_findings(ctx.artifact_path(findings))
        except ValueError:
            # A malformed artifact has already produced GARBAGE and cannot enter this gate loop.
            prior_findings = ()
    pending_memories = []
    if initial.outcome is Outcome.BLOCK:
        pending = capture_blocker(ctx, phase.id, initial.message, phase_type=phase.type)
        if pending is not None:
            pending_memories.append(pending)

    def revise(attempt: int) -> PhaseResult:
        ctx.gate_rounds_spent[phase.id] = spent + attempt
        ctx.on_event(events.retry(phase.id, spent + attempt, configured))
        if not route:
            return PhaseResult(Outcome.BLOCK, f"phase '{phase.id}' BLOCKed and has no on_block")
        if phase.selective_on_block:
            selected_ids = {
                finding.owner for finding in prior_findings if finding.blocks and finding.owner
            }
            selected = [
                candidate
                for candidate in ctx.config.phases
                if candidate.id in selected_ids and candidate.id in phase.selective_on_block
            ]
            if not selected:
                return PhaseResult(
                    Outcome.GARBAGE,
                    f"phase '{phase.id}' BLOCKed without a selectable finding owner",
                )
            lane_results = _run_parallel_producers(ctx, selected, findings=findings)
            for _lane, lane_result in lane_results:
                ctx.history.append(lane_result)
                if lane_result.outcome not in (Outcome.DONE, Outcome.PASS):
                    return lane_result
        # Follow the graph's back-edge, then traverse every configured phase normally until the
        # blocking gate. Only the first producer is primed with this gate's findings.
        #
        # Every *reviewer* on the route re-reads code that was just revised. Run fresh, each one
        # re-derives findings from scratch and reports whatever it happens to notice, so a round can
        # resolve every prior blocker and still be blocked by three brand-new ones — the ticket #19
        # treadmill. Handing them this gate's carried findings puts them in the same verification
        # mode the gate itself uses: confirm what was reported, and raise genuine late discovery as
        # advisory rather than as another blocker.
        result = PhaseResult(Outcome.DONE, "no revise phases ran")
        for index, retry_phase in enumerate(route):
            if index == 0 and retry_phase.type == "producer":
                checkpoint_error = _checkpoint_phases(ctx, [retry_phase])
                if checkpoint_error is not None:
                    return checkpoint_error
                result = _run_with_fresh_attempts(
                    ctx,
                    retry_phase,
                    lambda: _run_producer(ctx, retry_phase, findings=findings),
                )
            else:
                result = _run_phase(ctx, retry_phase, revise_findings=prior_findings)
            ctx.history.append(result)
            if result.outcome not in (Outcome.DONE, Outcome.PASS):
                return result
        return result

    def default_verify(attempt: int) -> PhaseResult:
        # Re-run the gating phase in verification mode (it reads its own findings + the revision).
        return _run_phase_for_verify(
            ctx,
            phase,
            prior_findings=prior_findings,
            gate_round=GateRound(
                index=spent + attempt,
                carried_ids=frozenset(finding.id for finding in prior_findings),
                final=(spent + attempt) >= configured,
            ),
        )

    base_verify = verify or default_verify

    def verify_and_emit(attempt: int) -> PhaseResult:
        # The verify verdict decides PASS (stop) vs BLOCK (retry again); without emitting it the run
        # log shows only the `retry` events and a silent gap where the verdict was — a reader can't
        # tell whether a later retry happened because verify BLOCKed or the loop misbehaved. Surface
        # each verify verdict exactly like the initial review's, so every PASS/BLOCK is visible.
        nonlocal prior_findings
        checkpoint_error = _checkpoint_phases(ctx, [phase])
        if checkpoint_error is not None:
            return checkpoint_error
        started = _emit_started(ctx, phase)
        result = base_verify(attempt)
        _emit_verdict_or_done(ctx, phase, result, started=started)
        if result.outcome is Outcome.BLOCK:
            if findings is not None and phase.structured_findings:
                try:
                    prior_findings = load_findings(ctx.artifact_path(findings))
                except ValueError:
                    pass
            pending = capture_blocker(ctx, phase.id, result.message, phase_type=phase.type)
            if pending is not None:
                pending_memories.append(pending)
        return result

    gate: GateResult = run_gate(
        initial=initial,
        revise=revise,
        verify=verify_and_emit,
        max_retries=budget,
    )
    if gate.passed:
        for pending in pending_memories:
            resolve_blocker(ctx, pending, verified_by=f"{phase.id}:PASS")
        return PhaseResult(Outcome.PASS, gate.final.message)
    return gate.final


def _retry_route(config: QuillfolioConfig, gate: PhaseDef) -> tuple[PhaseDef, ...]:
    """Phases traversed after following ``gate.on_block`` back to an earlier phase."""
    if not gate.on_block:
        return ()
    start = config.phase_ids.index(gate.on_block[0])
    stop = config.phase_ids.index(gate.id)
    return tuple(config.phases[start:stop])


def _gate_findings_artifact(ctx: RunContext, phase: PhaseDef) -> str | None:
    """The file a BLOCKing gate wrote its findings to, for the revise producer to read.

    Every gated phase points its revise at what failed, so no retry runs blind:

    * ``finalizer`` — writes a reconciled review to its own ``artifact``.
    * single-model ``reviewer`` — writes ``review-<id>-<slug>.md``.
    * mechanical ``build_test`` — writes the failing build/test log to ``build-findings.md`` (see
      :func:`quill.mechanical.step_build_test`).
    * mechanical ``ci_check`` — writes the failing CI jobs and their logs to ``ci-findings.md``.

    ``None`` only when a gate genuinely produces nothing to point at.
    """
    if phase.type == "finalizer":
        return phase.artifact or f"{phase.id}.md"
    if phase.type == "reviewer" and phase.model is not None:
        return _findings_name(phase, phase.model)
    if phase.type == "mechanical":
        from quill.mechanical import BUILD_FINDINGS_NAME, CI_FINDINGS_NAME

        return {
            "build": BUILD_FINDINGS_NAME,
            "test": BUILD_FINDINGS_NAME,
            "build_test": BUILD_FINDINGS_NAME,
            "ci_check": CI_FINDINGS_NAME,
        }.get(phase.step or "")
    return None


def _blocking_ids(
    ctx: RunContext, findings: tuple[Finding, ...], gate_round: GateRound
) -> frozenset[str]:
    """Which of ``findings`` actually stop this round, per the repository's blocking policy.

    The same question :func:`~quill.findings.deterministic_gate_result` answers when it computes the
    verdict. Asking it here keeps the prompt and the verdict on one account of what blocked.
    """
    policy = ctx.config.gates
    return frozenset(
        finding.id
        for finding in findings
        if policy.blocks_at(
            finding,
            round_index=gate_round.index,
            carried_ids=gate_round.carried_ids,
            final_round=gate_round.final,
        )
    )


def _run_phase_for_verify(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    prior_findings: tuple[Finding, ...] = (),
    gate_round: GateRound = GateRound(),
) -> PhaseResult:
    """Re-spawn the gating phase's persona to confirm the revision (narrow verification)."""
    if phase.type == "finalizer":
        artifact = phase.artifact or f"{phase.id}.md"
        reconciled: tuple[Finding, ...] = ()
        verification_findings = prior_findings
        if phase.structured_findings:
            try:
                reconciled = _load_reconciled_findings(ctx, phase)
            except ValueError as exc:
                return PhaseResult(Outcome.GARBAGE, str(exc))
            verification_findings = merge_verification_findings(prior_findings, reconciled)
        result = _spawn_llm(
            ctx,
            phase,
            model=phase.model,
            task=_finalizer_task(
                ctx,
                phase,
                verify=True,
                prior_findings=verification_findings,
                blocking_ids=_blocking_ids(ctx, verification_findings, gate_round),
            ),
        )
        repaired = _repair_artifact_failure(
            ctx, phase, model=phase.model, artifact=artifact, result=result
        )
        if phase.structured_findings:
            return _resolve_structured_gate(
                ctx,
                phase,
                model=phase.model,
                artifact=artifact,
                result=repaired,
                prior=verification_findings,
                gate_round=gate_round,
                verify=True,
            )
        return repaired
    findings = _findings_name(phase, phase.model or "")
    result = _spawn_llm(
        ctx,
        phase,
        model=phase.model,
        task=_review_task(
            ctx,
            phase,
            findings,
            verify=True,
            prior_findings=prior_findings,
            blocking_ids=_blocking_ids(ctx, prior_findings, gate_round),
        ),
    )
    repaired = _repair_artifact_failure(
        ctx, phase, model=phase.model, artifact=findings, result=result
    )
    if phase.structured_findings:
        return _resolve_structured_gate(
            ctx,
            phase,
            model=phase.model,
            artifact=findings,
            result=repaired,
            prior=prior_findings,
            gate_round=gate_round,
            verify=True,
        )
    return repaired


# -- spawning + prompt assembly ---------------------------------------------------


def _spawn_producer(
    ctx: RunContext, phase: PhaseDef, *, findings: str | None = None
) -> PhaseResult:
    """Spawn a producer/commit phase: write its ``artifact`` in the run dir.

    On a revise, ``findings`` names the reviewer's findings file so the producer's task points at it.
    """
    artifact = phase.artifact or f"{phase.id}.md"
    task = _producer_task(ctx, phase, artifact, findings=findings)
    if phase.id == "commit":
        with commit_attribution(ctx.directory, ctx.run_dir, phase.model or "unknown"):
            result = _spawn_llm(ctx, phase, model=phase.model, task=task)
    else:
        result = _spawn_llm(ctx, phase, model=phase.model, task=task)
    return _repair_artifact_failure(ctx, phase, model=phase.model, artifact=artifact, result=result)


def _repair_artifact_failure(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    model: str | None,
    artifact: str,
    result: PhaseResult,
) -> PhaseResult:
    """Give Pi one same-session chance to finish a missing artifact/receipt contract.

    Recovery is deliberately narrow: only a normal but unparsable response, or a success receipt
    whose required artifact is absent/empty, qualifies. Explicit FAILED/BLOCK outcomes and process
    crashes remain authoritative. The repaired turn is validated exactly like the original.
    """
    # Structured reviewer artifacts have their own schema validator and repair prompt. When one
    # exists, do not waste the single continuation merely repairing receipt prose: the subsequent
    # deterministic resolver will accept valid JSON or report/repair the exact schema defect.
    structured_path = ctx.artifact_path(artifact)
    if (
        (phase.structured_findings or artifact == PR_REVIEW_NAME)
        and result.outcome not in (Outcome.CRASH, Outcome.FAILED, Outcome.NEEDS_DECISION)
        and structured_path.is_file()
        and structured_path.stat().st_size > 0
    ):
        # A valid structured artifact needs no receipt repair, but an opted-in phase still gets its
        # self-check: the schema validator proves the JSON is well formed, never that a finding
        # inside it survives contact with the source it cites.
        return _self_check_phase(ctx, phase, model=model, artifact=artifact, result=result)
    checked = _require_artifact(ctx, result, artifact, max_chars=phase.max_artifact_chars)
    if checked.outcome is not Outcome.GARBAGE:
        return _self_check_phase(ctx, phase, model=model, artifact=artifact, result=checked)
    if ctx.deps.session_repair is None:
        return checked

    path = ctx.artifact_path(artifact)
    artifact_ready = path.is_file() and path.stat().st_size > 0
    try:
        artifact_chars = len(path.read_text(encoding="utf-8")) if artifact_ready else 0
    except (OSError, UnicodeError):
        artifact_chars = 0
    artifact_too_large = (
        artifact_ready
        and phase.max_artifact_chars is not None
        and artifact_chars > phase.max_artifact_chars
    )
    expected = (
        "one final receipt line beginning `PASS:`, `BLOCK:`, or `FAILED:`"
        if phase.gates
        else "one final receipt line beginning `DONE:` or `FAILED:`"
    )
    decision = (
        "Emit `PASS:` or `BLOCK:` according to the corrected artifact; emit `FAILED:` only if "
        "you cannot complete the phase contract."
        if phase.gates
        else "Emit `DONE:` only if the full contract is complete; otherwise emit `FAILED:`."
    )
    if artifact_too_large:
        instruction = (
            f"Quill rejected your phase output: {checked.message}. Your artifact at {path} "
            f"exceeds its {phase.max_artifact_chars:,}-character handoff "
            "limit. Compact it in place: preserve requirements, decisions, evidence, blocking "
            "findings, and actionable handoffs; remove narration, repetition, history, and copied "
            f"source. Quill expects {expected}. Re-evaluate the original task. If incomplete, "
            f"continue the work before deciding. {decision}"
        )
    elif artifact_ready:
        instruction = (
            f"Quill rejected your phase output: {checked.message}. An artifact exists at {path}, "
            f"but your final receipt was missing or invalid. Quill expects {expected}. "
            "Re-evaluate the original task and inspect the current artifact/work. Its existence "
            "does not prove completion. If incomplete, continue working now. Only when the full "
            f"contract has been re-evaluated should you decide the verdict. {decision}"
        )
    else:
        instruction = (
            f"Quill rejected your phase output: {checked.message}. You returned or described the "
            "deliverable in chat but did not complete the required "
            f"artifact contract. Continue the original task and write the complete deliverable "
            f"to {path}. Quill expects {expected}. Re-evaluate completion, then emit the task's "
            f"final receipt. {decision}"
        )

    fix_started = _self_fix_started(ctx, phase)
    repaired = _repair_llm_session(ctx, phase, model=model, prompt=instruction)
    if repaired.outcome is Outcome.FAILED:
        _self_fix_done(ctx, phase, fix_started, repaired=False)
        return _self_fix_failure(checked.message, repaired)
    checked = _require_artifact(ctx, repaired, artifact, max_chars=phase.max_artifact_chars)
    _self_fix_done(ctx, phase, fix_started, repaired=_self_fix_repaired(checked))
    return _self_check_phase(ctx, phase, model=model, artifact=artifact, result=checked)


def _self_fix_failure(problem: str, result: PhaseResult) -> PhaseResult:
    """Keep an unsuccessful corrective turn eligible for a fresh phase attempt."""
    detail = result.message or result.outcome.value
    return PhaseResult(
        Outcome.GARBAGE,
        f"same-session self-fix did not repair malformed output ({problem}): {detail}",
        raw_receipt=result.raw_receipt,
    )


def _resolve_structured_review(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    model: str | None,
    artifact: str,
    result: PhaseResult,
    namespace: str,
) -> PhaseResult:
    """Validate and namespace a non-gating reviewer artifact with bounded contract repair."""
    path = ctx.artifact_path(artifact)
    if result.outcome in (Outcome.CRASH, Outcome.FAILED, Outcome.NEEDS_DECISION):
        decided = result
    else:
        checked = _require_artifact(
            ctx,
            PhaseResult(Outcome.DONE, raw_receipt=result.raw_receipt),
            artifact,
            max_chars=phase.max_artifact_chars,
        )
        decided = (
            checked
            if checked.outcome is Outcome.GARBAGE
            else deterministic_review_result(path, result, namespace=namespace)
        )
    for attempt in range(1, _STRUCTURED_REPAIR_ATTEMPTS + 1):
        if decided.outcome is not Outcome.GARBAGE or ctx.deps.session_repair is None:
            return decided
        instruction = (
            f"Quill rejected the structured findings artifact at {path}: {decided.message}. "
            f"Contract repair {attempt}/{_STRUCTURED_REPAIR_ATTEMPTS}. Repair that JSON file in "
            "place. Preserve every substantive finding. Every finding must contain non-empty "
            "string fields: id, severity, status, title, requirement, evidence, failure_scenario, "
            "and required_outcome. Use schema_version 1, valid severity/status values from the "
            "original contract, and no Markdown or extra prose. Then emit the original task's "
            "DONE receipt."
        )
        fix_started = _self_fix_started(ctx, phase)
        repaired = _repair_llm_session(ctx, phase, model=model, prompt=instruction)
        if repaired.outcome in (Outcome.CRASH, Outcome.NEEDS_DECISION):
            _self_fix_done(ctx, phase, fix_started, repaired=False)
            return repaired
        if repaired.outcome is Outcome.FAILED:
            _self_fix_done(ctx, phase, fix_started, repaired=False)
            return _self_fix_failure(decided.message, repaired)
        checked = _require_artifact(
            ctx,
            PhaseResult(Outcome.DONE, raw_receipt=repaired.raw_receipt),
            artifact,
            max_chars=phase.max_artifact_chars,
        )
        decided = (
            checked
            if checked.outcome is Outcome.GARBAGE
            else deterministic_review_result(path, repaired, namespace=namespace)
        )
        _self_fix_done(ctx, phase, fix_started, repaired=_self_fix_repaired(decided))
    return decided


def _resolve_structured_gate(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    model: str | None,
    artifact: str,
    result: PhaseResult,
    prior: tuple[Finding, ...] = (),
    gate_round: GateRound = GateRound(),
    verify: bool = False,
) -> PhaseResult:
    """Validate a structured gate and repair its contract in the same Pi session.

    ``verify`` marks a re-review, whose artifact Quill reassembles from the prior findings it
    already holds (see :func:`~quill.findings.materialize_verification_delta`).
    """
    delta = verify and phase.gates
    path = ctx.artifact_path(artifact)

    def decide(outcome: PhaseResult) -> PhaseResult:
        checked = _require_artifact(
            ctx,
            PhaseResult(Outcome.DONE, raw_receipt=outcome.raw_receipt),
            artifact,
            max_chars=phase.max_artifact_chars,
        )
        if checked.outcome is Outcome.GARBAGE:
            return checked
        if delta and prior:
            # A verification pass answers in the status-delta shape; fold it back into the
            # canonical array using the findings Quill already holds, so prior identity is never
            # re-emitted and therefore cannot drift. A full-array answer passes through untouched.
            try:
                materialize_verification_delta(path, prior)
            except ValueError as exc:
                return PhaseResult(Outcome.GARBAGE, str(exc), raw_receipt=outcome.raw_receipt)
        return deterministic_gate_result(
            path,
            outcome,
            prior=prior,
            allowed_owners=phase.selective_on_block,
            policy=ctx.config.gates,
            round_index=gate_round.index,
            carried_ids=gate_round.carried_ids,
            final_round=gate_round.final,
        )

    if result.outcome in (Outcome.CRASH, Outcome.FAILED, Outcome.NEEDS_DECISION):
        decided = result
    else:
        decided = decide(result)
    prior_line = _prior_findings_instruction(
        prior, delta=delta, blocking_ids=_blocking_ids(ctx, prior, gate_round)
    )
    for attempt in range(1, _STRUCTURED_REPAIR_ATTEMPTS + 1):
        if decided.outcome is not Outcome.GARBAGE or ctx.deps.session_repair is None:
            return decided
        shape = (
            (
                "Repair that JSON file in place using the status-delta shape: "
                '{"schema_version":1,"dispositions":[{"id":"<a prior id>","status":"RESOLVED|OPEN",'
                '"evidence":"path:line"}],"new_findings":[<full finding objects>]}. '
                "Every id in dispositions must be one of the prior IDs listed above; do not invent "
                "IDs and do not restate a prior finding's other fields."
            )
            if delta
            else (
                "Repair that JSON file in place. Preserve every substantive finding and every prior "
                "finding identity. Every finding must contain non-empty string fields: id, "
                "severity, status, title, requirement, evidence, failure_scenario, and "
                "required_outcome. Preserve owner and introduced_by_revision when present."
            )
        )
        instruction = (
            f"Quill rejected the structured findings artifact at {path}: {decided.message}. "
            f"Contract repair {attempt}/{_STRUCTURED_REPAIR_ATTEMPTS}. {shape} Use schema_version "
            "1, valid severity/status values from the original contract, and no Markdown or extra "
            f"prose.{prior_line} Then emit the original task's PASS or BLOCK receipt."
        )
        fix_started = _self_fix_started(ctx, phase)
        repaired = _repair_llm_session(ctx, phase, model=model, prompt=instruction)
        if repaired.outcome in (Outcome.CRASH, Outcome.NEEDS_DECISION):
            _self_fix_done(ctx, phase, fix_started, repaired=False)
            return repaired
        if repaired.outcome is Outcome.FAILED:
            _self_fix_done(ctx, phase, fix_started, repaired=False)
            return _self_fix_failure(decided.message, repaired)
        decided = decide(repaired)
        _self_fix_done(ctx, phase, fix_started, repaired=_self_fix_repaired(decided))
    return decided


def _self_fix_started(ctx: RunContext, phase: PhaseDef) -> float:
    """Mark one malformed-output repair attempt as active and return its clock origin."""
    started = time.monotonic()
    _emit_event(ctx, events.self_fix_started(phase.id, phase.label or phase.id))
    return started


def _self_fix_done(ctx: RunContext, phase: PhaseDef, started: float, *, repaired: bool) -> None:
    """Publish the deterministic result after the repaired output has been revalidated."""
    _emit_event(
        ctx,
        events.self_fix_done(
            phase.id,
            phase.label or phase.id,
            repaired=repaired,
            duration_s=round(time.monotonic() - started, 2),
        ),
    )


def _self_fix_repaired(result: PhaseResult) -> bool:
    return result.outcome in (Outcome.DONE, Outcome.PASS, Outcome.BLOCK)


#: A self-check runs after any verdict it could still improve. BLOCK is included so a gate can
#: re-verify its own findings against source before the workflow pays for another producer round.
_SELF_CHECK_OUTCOMES = (Outcome.DONE, Outcome.PASS, Outcome.BLOCK)


def _self_check_prompt(ctx: RunContext, phase: PhaseDef, *, artifact: str) -> str:
    """The continuation prompt for one self-check, from the phase's persona or the generic text.

    The worker is already inside the phase's session, so neither variant restates the preamble or
    the original task: it names the artifact and the standard the check is against.
    """
    location = (
        f"Your artifact for this phase is at {ctx.artifact_path(artifact)}. This is the only "
        "self-check iteration for this phase attempt. Correct the artifact or the repository work "
        "directly, then emit the receipt line your original task requires."
    )
    if phase.self_check_persona is not None:
        body = load_persona_body(ctx.config.persona_path(phase.self_check_persona))
        return f"{body}\n\n{location}"
    return (
        "Run one bounded completion self-check. Re-read the ticket, your phase instructions, your "
        "required skills, the current repository state, and your artifact. Check phase-contract "
        "and skill conformance, factual accuracy, unsupported or invented claims, missed "
        "requirements, and obvious correctness gaps. Do not broaden scope or repeat the "
        f"independent review phase. {location}"
    )


def _self_check_phase(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    model: str | None,
    artifact: str,
    result: PhaseResult,
) -> PhaseResult:
    """Run one non-gating completion-and-correction pass in the producer's Pi session."""
    if not phase.self_check or result.outcome not in _SELF_CHECK_OUTCOMES:
        return result
    if ctx.deps.session_repair is None:
        return result

    label = phase.label or phase.id
    started = time.monotonic()
    _emit_event(ctx, events.self_check_started(phase.id, label))
    prompt = _self_check_prompt(ctx, phase, artifact=artifact)
    checked = _repair_llm_session(ctx, phase, model=model, prompt=prompt)
    checked = _require_artifact(ctx, checked, artifact, max_chars=phase.max_artifact_chars)
    _emit_event(
        ctx,
        events.self_check_done(
            phase.id,
            label,
            verdict=_verdict_of(checked),
            duration_s=round(time.monotonic() - started, 2),
        ),
    )
    # A self-check is a bounded correction opportunity, not another gate. Repository and artifact
    # changes made by the continuation persist, while the producer's successful verdict remains
    # authoritative. A later reviewer may still send the workflow back for a fresh producer attempt,
    # which receives its own single self-check.
    return result


def _repair_llm_session(
    ctx: RunContext, phase: PhaseDef, *, model: str | None, prompt: str
) -> PhaseResult:
    """Run one corrective prompt in the Pi session immediately preceding it."""
    repair = ctx.deps.session_repair
    if repair is None:  # guarded by the caller; retained for type narrowing and direct safety
        return PhaseResult(Outcome.GARBAGE, "runner does not support same-session repair")
    model_name = model or ""
    stream_path = _stream_path(ctx, phase, model)
    on_tool, on_usage = _phase_callbacks(ctx, phase, stream_path, continuation=True)
    try:
        stdout = repair(
            phase.id,
            model_name,
            prompt,
            timeout=ctx.config.opencode_run_seconds,
            stream_path=stream_path,
            on_tool=on_tool,
            on_usage=on_usage,
        )
    except (SpawnTimeout, SpawnError) as exc:
        return PhaseResult(Outcome.CRASH, f"same-session continuation failed: {exc}")
    return classify_receipt(ctx.deps.extract(stdout))


def _require_artifact(
    ctx: RunContext,
    result: PhaseResult,
    artifact: str,
    *,
    max_chars: int | None = None,
) -> PhaseResult:
    """Downgrade a successful receipt to GARBAGE if its declared artifact wasn't written.

    A worker can return ``DONE``/``PASS`` without actually writing the file it was told to
    produce (the model narrates success but skips the write). The receipt is not the source of
    truth — the file on disk is. If a phase claims success but its artifact is missing or empty,
    treat it as GARBAGE so the run fails loudly on the real cause instead of the next phase
    imploding on a file that isn't there.

    Only successful outcomes are checked: a FAILED/BLOCK/CRASH/NEEDS_DECISION result already
    carries its own reason and shouldn't be masked by an artifact complaint.
    """
    if result.outcome not in (Outcome.DONE, Outcome.PASS):
        return result
    path = ctx.run_dir / artifact
    if path.is_file() and path.stat().st_size > 0:
        if max_chars is None:
            return result
        try:
            chars = len(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            return PhaseResult(
                Outcome.GARBAGE,
                message=f"phase artifact '{artifact}' could not be read: {exc}",
                raw_receipt=result.raw_receipt,
            )
        if chars <= max_chars:
            return result
        return PhaseResult(
            Outcome.GARBAGE,
            message=(
                f"phase artifact '{artifact}' is {chars:,} characters; limit is {max_chars:,}"
            ),
            raw_receipt=result.raw_receipt,
        )
    reason = "empty" if path.is_file() else "missing"
    return PhaseResult(
        Outcome.GARBAGE,
        message=f"phase reported {result.outcome.value} but its artifact '{artifact}' is {reason}",
        raw_receipt=result.raw_receipt,
    )


def _spawn_llm(ctx: RunContext, phase: PhaseDef, *, model: str | None, task: str) -> PhaseResult:
    """Load ``model``, assemble the full prompt, spawn, and classify the receipt.

    The worker's live JSONL stream is tee'd to ``<run-dir>/stream-<phase>-<model>.jsonl`` so the
    run dir holds a live, tail-able transcript of every spawn (thinking, tool calls, tokens),
    alongside ``pipeline-run.log``.
    """
    prompt = assemble_prompt(ctx, phase, task)
    model_name = model or ""
    label = phase.label or phase.id
    load_failure = _prepare_model(
        ctx,
        phase,
        model_name,
        inside_phase_attempt=True,
    )
    if load_failure is not None:
        return load_failure
    stream_path = _stream_path(ctx, phase, model)
    on_tool, on_usage = _phase_callbacks(ctx, phase, stream_path)
    result = run_preloaded_phase(
        spawn=ctx.deps.spawn,
        preset=model_name,
        agent=phase.id,
        prompt=prompt,
        timeout=ctx.config.opencode_run_seconds,
        stream_path=stream_path,
        extract=ctx.deps.extract,
        on_tool=on_tool,
        on_usage=on_usage,
        on_model_loaded=lambda: _emit_event(
            ctx, events.phase_executing(phase.id, label, model=model_name)
        ),
    )
    # A worker CRASH does not imply the successfully prepared model was unloaded. Preserve the
    # affinity hint so a fresh phase attempt does not fabricate another model-switch operation.
    return result


def _spawn_preloaded_llm(ctx: RunContext, phase: PhaseDef, *, model: str, task: str) -> PhaseResult:
    """Spawn one audit lane after its review group prepared the shared model once."""
    prompt = assemble_prompt(ctx, phase, task)
    label = phase.label or phase.id
    stream_path = _stream_path(ctx, phase, model)
    on_tool, on_usage = _phase_callbacks(ctx, phase, stream_path)
    return run_preloaded_phase(
        spawn=ctx.deps.spawn,
        preset=model,
        agent=phase.id,
        prompt=prompt,
        timeout=ctx.config.opencode_run_seconds,
        stream_path=stream_path,
        extract=ctx.deps.extract,
        on_tool=on_tool,
        on_usage=on_usage,
        on_model_loaded=lambda: _emit_event(
            ctx, events.phase_executing(phase.id, label, model=model)
        ),
    )


# -- tool-call tally --------------------------------------------------------------
#
# A phase's spawn prints nothing between `phase_started` and `phase_done`, so a long phase is
# indistinguishable from a hung one at the terminal (observed: a 41-minute impl phase, 31 minutes
# of it dead spawns, diagnosable only by hand-parsing the JSONL transcript). We tally each tool the
# worker starts, tick a live in-place counter, and hand the totals to `phase_done` as the phase's
# permanent record in the log.
#
# The tally lives on the RunContext (per-run) and accumulates across a gate's revise→verify rounds:
# `_gate` re-runs the same producer, and one line per phase covering all its tool calls is what a
# reader wants — not a tally that silently resets on each retry.


def _phase_callbacks(
    ctx: RunContext, phase: PhaseDef, stream_path: Path, *, continuation: bool = False
) -> tuple[Callable[[str], None], Callable[[LiveUsage], None]]:
    """Build live tool and usage counters for ``phase``."""
    tool_counter = _tool_counter(ctx, phase.id, stream_path)
    usage_counter = _usage_counter(ctx, phase.id, stream_path, continuation=continuation)
    return tool_counter, usage_counter


def _tool_counter(ctx: RunContext, phase_id: str, stream_path: Path) -> Callable[[str], None]:
    """An ``on_tool`` callback that tallies calls for ``phase_id`` and ticks the live counter.

    ``stream_path`` is forwarded to the progress hook so a consumer (the API service) can read the
    phase's running token usage from the live transcript and push it out in real time.
    """

    def bump(name: str) -> None:
        with ctx.event_lock:
            tally = ctx.tool_tally.setdefault(phase_id, {})
            tally[name] = tally.get(name, 0) + 1
            snapshot = dict(tally)
        progress = ctx.deps.on_tool_progress
        if progress is not None:
            progress(phase_id, snapshot, stream_path)

    return bump


def _usage_counter(
    ctx: RunContext, phase_id: str, stream_path: Path, *, continuation: bool = False
) -> Callable[[LiveUsage], None]:
    """Convert one spawn into processed totals and non-duplicated context occupancy."""
    base = ctx.phase_usage.get(phase_id, LiveUsage())
    prior_session = (
        ctx.phase_session_usage.get(phase_id, LiveUsage()) if continuation else LiveUsage()
    )

    def update(spawn_usage: LiveUsage) -> None:
        phase_usage = LiveUsage(
            base.input_tokens + spawn_usage.input_tokens,
            base.output_tokens + spawn_usage.output_tokens,
            max(0, base.context_window_tokens - prior_session.context_window_tokens)
            + spawn_usage.context_window_tokens,
        )
        with ctx.event_lock:
            ctx.phase_usage[phase_id] = phase_usage
            ctx.phase_session_usage[phase_id] = spawn_usage
        progress = ctx.deps.on_usage_progress
        if progress is not None:
            progress(phase_id, phase_usage, stream_path)

    return update


def _take_tools(ctx: RunContext, phase_id: str) -> dict[str, int] | None:
    """The phase's tool tally, cleared so a later re-run starts fresh. ``None`` if it ran none."""
    with ctx.event_lock:
        return ctx.tool_tally.pop(phase_id, None) or None


def _stream_path(ctx: RunContext, phase: PhaseDef, model: str | None) -> Path:
    """Run-dir file the spawn's live JSONL stream is tee'd to. Per (phase, model) so a fan-out
    reviewer's two models don't collide."""
    slug = slugify(model or "") or "model"
    key = f"{phase.id}:{slug}"
    with ctx.event_lock:
        sequence = ctx.transcript_counts.get(key, 0) + 1
        candidate = ctx.run_dir / f"stream-{phase.id}-{slug}-{sequence}.jsonl"
        # A phase restart carries earlier transcripts into the linked run. Its in-memory counter
        # starts fresh, so advance past inherited names instead of overwriting prior evidence.
        while candidate.exists():
            sequence += 1
            candidate = ctx.run_dir / f"stream-{phase.id}-{slug}-{sequence}.jsonl"
        ctx.transcript_counts[key] = sequence
    return candidate


def assemble_prompt(ctx: RunContext, phase: PhaseDef, task: str) -> str:
    """Build the full spawn prompt: persona + ticket + path injection + task + skill directive.

    The persona is loaded under the shared PREAMBLE (path-agnostic). The engine states the run
    dir here — personas carry no ``{results_dir}`` token (#33 decision 1). The ticket is fetched
    once (branch phase) and stashed on ``ctx``; we inject it into every phase so reviewers and
    finalizers judge against the goal without each re-fetching it.

    quill stores no skill bodies: a phase's ``skills = [...]`` config is just names. If it lists
    any, the runner formats a load directive in its own trigger syntax (pi ``/skill:x``, opencode
    ``/x``). The directive is placed BEFORE the task so the TASK line is the LAST thing the worker
    reads: a runner (pi) that expands ``/skill:x`` into a full SKILL.md body would otherwise bury
    the task under a wall of generic skill text, and a small model reads the trailing skill doc as
    "a skill was loaded, no task was given" and stalls asking what to do (observed on
    Qwen3.6_27B_FP8, branch phase). Skill-load then task keeps the imperative at the generation
    point.
    """
    persona = load_persona(ctx.config.persona_path(phase.persona)) if phase.persona else ""
    ticket_block = _ticket_block(ctx)
    run_dir = _abs_run_dir(ctx)
    path_block = (
        f"RUN DIR: {run_dir}\n"
        f"Write every artifact and findings file inside that run directory. "
        f"The file paths your task names are ABSOLUTE — write to exactly those paths, do not "
        f"resolve them against any other directory.\n"
    )
    skill_line = ctx.deps.skill_directive(list(phase.skills))
    skill_block = f"{skill_line}\n\n" if skill_line else ""
    memory_block = verified_memory_block(ctx, phase.id)
    return f"{persona}\n\n{ticket_block}{path_block}\n{memory_block}{skill_block}{task}"


def _ensure_ticket(ctx: RunContext) -> bool:
    """Guarantee ``ctx.body`` is populated before any phase runs; return False if not.

    The branch phase normally fetches it. On ``--resume``/``--start-phase`` past that phase it is
    empty, so we fetch here. An empty body means the run has no goal to plan, implement, or review
    against — the caller fails the run rather than spawning blind agents.
    """
    if ctx.body:
        return True
    git = ctx.deps.git
    if git is None:
        return False
    try:
        ctx.body = git.issue_body(ctx.ticket)
    except Exception:  # noqa: BLE001 - any issue-provider failure makes context unavailable
        return False
    if not ctx.body.strip():
        return False
    if not ctx.title:
        ctx.title = _title_from(ctx.body) or f"ticket {ctx.ticket}"
    return True


def _ensure_pr_target(ctx: RunContext) -> str | None:
    """Resolve the open PR an update or review run targets.

    Update mode also captures feedback created or edited strictly after the head commit. Review
    mode needs the same immutable PR boundary but deliberately reviews the complete ticket/diff.
    """
    if ctx.mode not in {MODE_UPDATE, MODE_REVIEW}:
        return None
    git = ctx.deps.git
    if git is None:
        return f"{ctx.mode} mode needs GitHub access, but no git/gh reader is wired"
    try:
        pr = git.pr_target_for_ticket(ctx.ticket)
    except Exception as exc:  # noqa: BLE001 - normalize backend failures for the run result
        return f"could not search for ticket #{ctx.ticket}'s open PR: {exc}"
    if pr is None:
        return (
            f"no open PR found for ticket #{ctx.ticket} — nothing to {ctx.mode}. "
            f"Run `quill {ctx.ticket}` to ship it from scratch."
        )
    if pr.head_sha:
        try:
            local_head = git.local_head_sha()
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            return f"could not verify the checked-out PR head: {exc}"
        if local_head != pr.head_sha:
            return (
                f"PR #{pr.number} moved while the update workspace was being prepared "
                f"({local_head[:12]} checked out, {pr.head_sha[:12]} remote); refresh and restart."
            )
    ctx.pr_number = pr.number
    ctx.pr_url = pr.url or ctx.pr_url
    ctx.branch = pr.branch
    ctx.pr_head_sha = pr.head_sha
    ctx.pr_head_committed_at = pr.committed_at
    if ctx.mode == MODE_REVIEW:
        return None
    try:
        snapshot = git.feedback_snapshot(pr)
    except Exception:  # noqa: BLE001 - normalize backend failures for the run result
        # The PR resolved but its comments didn't. Losing feedback silently would produce an
        # "update" that re-implements the same code with nothing to act on, so say so and stop.
        return f"found PR #{pr.number} for ticket #{ctx.ticket} but could not read its comments"
    if not snapshot.selected:
        short = pr.head_sha[:12] or "unknown head"
        return f"No PR feedback was created or edited after `{short}`."
    ctx.feedback = snapshot.render_prompt()
    ctx.feedback_ids = tuple(item.id for item in snapshot.selected)
    ctx.feedback_threads = tuple(
        item.thread_id
        for item in snapshot.selected
        if item.thread_id and item.resolved is False and item.viewer_can_resolve is True
    )
    payload = {
        "pr": {
            "number": pr.number,
            "url": pr.url,
            "branch": pr.branch,
            "head_sha": pr.head_sha,
            "committed_at": pr.committed_at,
        },
        "selection_rule": "actionable_at > head committedDate",
        "selected": [
            {
                "id": item.id,
                "source": item.source,
                "author": item.author,
                "body": item.body,
                "actionable_at": item.actionable_at,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "state": item.state,
                "path": item.path,
                "line": item.line,
                "thread_id": item.thread_id,
                "resolved": item.resolved,
                "viewer_can_resolve": item.viewer_can_resolve,
                "url": item.url,
            }
            for item in snapshot.selected
        ],
        "excluded": {
            "at_or_before_boundary": snapshot.excluded_old,
            "blank_body": snapshot.excluded_blank,
            "malformed_timestamp": snapshot.excluded_malformed,
        },
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "pr-feedback.json").write_text(encoded, encoding="utf-8")
    (ctx.run_dir / "pr-feedback.md").write_text(ctx.feedback + "\n", encoding="utf-8")
    ctx.feedback_digest = hashlib.sha256(encoded.encode()).hexdigest()
    return None


def _ticket_block(ctx: RunContext) -> str:
    """The ticket's title + body as a context block, injected into every phase.

    ``ctx.body`` is fetched and validated once in :func:`run_phases`; by the time any phase
    assembles its prompt it is guaranteed non-empty.

    In update mode the block also carries the open PR's review feedback. That feedback — not the
    ticket alone — is the goal of an update run: the plan phase re-plans against it, the impl
    phase fixes it, and the reviewers judge whether it was addressed. Injecting it into every
    phase (like the ticket) means no phase has to re-fetch or infer it.
    """
    title = ctx.title or f"ticket {ctx.ticket}"
    body = body_text_from(ctx.body)
    return f"TICKET #{ctx.ticket}: {title}\n{body}\n\n{_pr_context_block(ctx)}"


def _pr_context_block(ctx: RunContext) -> str:
    """The existing-PR context for update and review workflows."""
    if ctx.pr_number is None:
        return ""
    if ctx.mode == MODE_REVIEW:
        return (
            f"PULL REQUEST REVIEW MODE — independently audit PR #{ctx.pr_number} "
            f"({ctx.pr_url or 'url unknown'}) at exact head {ctx.pr_head_sha}. The checked-out "
            f"branch is '{ctx.branch}'. Compare the complete PR diff from its GitHub merge base "
            "against the ticket and repository contracts. Read the PR description and relevant "
            "comments. Do not modify any repository file, commit, push, approve, request changes, "
            "or post comments; Quill Python owns publication.\n\n"
        )
    if ctx.mode != MODE_UPDATE:
        return ""
    feedback = ctx.feedback.strip()
    return (
        f"UPDATE MODE — PR #{ctx.pr_number} ({ctx.pr_url or 'url unknown'}) on branch "
        f"'{ctx.branch}' is already open for this ticket and has been reviewed. This run REVISES "
        f"that PR; it does not start over. The work already on that branch stands unless the "
        f"feedback below says otherwise.\n"
        # Stated for every phase, not just the branch one: the engine has no notion of which
        # configured phase touches git (that is persona data), so the rule has to travel with the
        # run. A phase that never runs git simply ignores it.
        f"Work on the EXISTING branch '{ctx.branch}' — check it out if you are not on it "
        f"(`git fetch origin {ctx.branch}` then `git checkout {ctx.branch}`). Do NOT create a new "
        f"branch, and do NOT open a second pull request: push to '{ctx.branch}' and PR "
        f"#{ctx.pr_number} updates itself.\n\n"
        f"ACTIVE PR FEEDBACK AFTER {ctx.pr_head_sha[:12]} (address every stable ID):\n"
        f"{feedback}\n\n"
    )


def _abs_run_dir(ctx: RunContext) -> str:
    """The run dir as an absolute POSIX path for the prompt.

    The worker's file-writing tool (opencode's ``write``) resolves relative paths against its own
    project-root detection — NOT the spawn's cwd — so a bare ``plan.md`` lands in the repo root or
    the user's home instead of the run dir, and the engine then reports the artifact missing. We
    inject absolute paths so there is no directory to resolve against.
    """
    return ctx.run_dir.resolve().as_posix()


def _abs_artifact(ctx: RunContext, name: str) -> str:
    """Absolute POSIX path of an artifact/findings file inside the run dir (see :func:`_abs_run_dir`)."""
    return (ctx.run_dir.resolve() / name).as_posix()


# -- task lines -------------------------------------------------------------------


def _producer_task(
    ctx: RunContext,
    phase: PhaseDef,
    artifact: str,
    *,
    findings: str | None = None,
    finding_owner: str | None = None,
) -> str:
    task = (
        f"TASK: {phase.label or phase.id} for ticket {ctx.ticket} (see TICKET block above). "
        f"Write your artifact to {_abs_artifact(ctx, artifact)}."
    )
    inputs = [_abs_artifact(ctx, name) for name in _input_artifacts(ctx, phase)]
    if inputs:
        task += f" Required handoff inputs: {', '.join(inputs)}."
    if phase.synthesizes:
        task += (
            " Produce one canonical synthesis from the latest version of every handoff input. "
            "Replace superseded lane conclusions in their existing sections; do not append stale "
            "and current conclusions or hide contradictions. Preserve evidence ownership and mark "
            "unresolved conflicts explicitly for the gate."
        )
    if phase.max_artifact_chars is not None:
        task += (
            f" Keep the artifact under {phase.max_artifact_chars:,} characters; preserve decisions "
            "and actionable evidence, not narration or copied context."
        )
    if findings:
        # This is a REVISE pass: a reviewer BLOCKed the prior artifact and wrote why. Without naming
        # that file the producer re-writes blind and reproduces the same gap, so the reviewer
        # re-BLOCKs on the identical finding until the retry budget is spent (observed: plan review
        # BLOCKing 3x on the same missing test). Point the producer at the findings and require it
        # to address each one, editing the existing artifact rather than starting over.
        # A synthesizer holds no evidence of its own — it reconciles lane artifacts. Telling it to
        # "address EVERY Critical/Major finding" leaves it only one way to comply: assert a
        # resolution it cannot investigate. Observed on ticket #20, where the same round told the
        # lane "address only findings whose owner is 'research_technical'" and told synthesis to
        # address every one, and the gate then rejected the result as "asserted correctness by
        # pattern-matching". Scope each producer to what it can actually substantiate.
        if finding_owner:
            owner_line = f" Address only findings whose owner is '{finding_owner}'; other lanes own the rest."
        elif phase.synthesizes:
            owner_line = (
                " The lanes own the blocking findings; carry their current conclusions through "
                "faithfully and leave any finding their artifacts do not yet resolve visibly open. "
                "Do not resolve a finding the lane artifacts do not support."
            )
        else:
            owner_line = " Address EVERY Critical/Major finding it lists (Minor/Nit are optional)."
        action = (
            "Regenerate the canonical synthesis from all latest lane artifacts."
            if phase.synthesizes
            else "Patch the existing artifact in place."
        )
        task += (
            f" This is a REVISION: a reviewer BLOCKED the previous version. Read its findings at "
            f"{_abs_artifact(ctx, findings)}.{owner_line} {action} Replace obsolete content; do not "
            "append review history or a finding-resolution narrative."
        )
    if phase.id == "commit":
        if ctx.mode == MODE_UPDATE:
            task += " In update mode, you may comment on the existing PR to answer its feedback."
        else:
            task += (
                " Do not comment on an existing PR in create mode, including during CI retries; "
                "the new commit message is the change summary."
            )
    return task


def _input_artifacts(ctx: RunContext, phase: PhaseDef) -> list[str]:
    """Resolve a producer's explicit handoff phase ids to artifact filenames."""
    out: list[str] = []
    for target_id in (*phase.inputs, *phase.synthesizes):
        target = ctx.config.phase(target_id)
        if target is not None and target.artifact:
            out.append(target.artifact)
    return out


# Verification is a property of the gate's revise→verify loop, not of any persona. Every re-pass must
# confirm the prior blocking findings, then guard against regressions introduced by the revision.
# Severity keeps fresh cosmetic observations from consuming the retry budget while still allowing a
# newly introduced correctness failure to block. The engine injects this baseline even when a
# user-edited persona omits verification guidance.
_VERIFY_INSTRUCTION = (
    "You are RE-REVIEWING a revision. For each prior finding, decide only whether current evidence "
    "proves its required outcome: RESOLVED if it does, OPEN if it does not. Perform a bounded "
    "regression audit only on the revised portions for regressions introduced by that revision. A "
    "new CRITICAL/MAJOR finding may block only when introduced_by_revision names the exact revised "
    "section or repository change that caused it. A pre-existing issue missed by the initial "
    "review is late discovery: report it as advisory MINOR/NIT instead of consuming another retry."
)

#: Verification output contract. Quill holds the prior findings, so a re-review reports a status
#: and evidence per prior ID rather than re-emitting nine immutable string fields per finding —
#: transcription is where small models actually fail, and a single drifted character used to
#: discard the whole run.
_STRUCTURED_DELTA_INSTRUCTION = (
    " Write exactly one JSON object with no Markdown: "
    '{"schema_version":1,"dispositions":[{"id":"<a prior id>","status":"RESOLVED|OPEN",'
    '"evidence":"path:line and observed fact"}],"new_findings":[{"id":"stable-id",'
    '"severity":"CRITICAL|MAJOR|MINOR|NIT","status":"OPEN","title":"concise defect",'
    '"requirement":"required behavior","evidence":"path:line and observed fact",'
    '"failure_scenario":"reachable impact","required_outcome":"behavior required",'
    '"introduced_by_revision":"the exact revised section that caused it"}]}. '
    "Do not restate a prior finding's title, requirement, severity, failure_scenario, or "
    "required_outcome — Quill already holds them and will reject an id it does not know. Omitting "
    "a prior id simply leaves it OPEN. Use empty arrays when there is nothing to report. Quill "
    "computes PASS or BLOCK from this file; the receipt does not decide the gate."
)

_STRUCTURED_FINDINGS_INSTRUCTION = (
    " Write exactly one JSON object with no Markdown: "
    '{"schema_version":1,"findings":[{"id":"stable-id","severity":"CRITICAL|MAJOR|MINOR|NIT",'
    '"status":"OPEN|RESOLVED","title":"concise defect","requirement":"required behavior",'
    '"evidence":"path:line and observed fact","failure_scenario":"reachable impact",'
    '"required_outcome":"behavior required"}]}. '
    "A finding may also include owner and introduced_by_revision string fields when the task "
    "requires them. Use an empty findings array when nothing is found. Quill computes PASS or BLOCK from this file; "
    "the receipt does not decide the gate."
)


def _review_task(
    ctx: RunContext,
    phase: PhaseDef,
    findings: str,
    *,
    verify: bool = False,
    prior_findings: tuple[Finding, ...] = (),
    blocking_ids: frozenset[str] | None = None,
) -> str:
    mode = "VERIFICATION mode" if verify else "review mode"
    verify_line = f" {_VERIFY_INSTRUCTION}" if verify else ""
    against = [_abs_artifact(ctx, a) for a in _against_artifacts(ctx, phase)]
    against_line = f" Review against these artifacts: {', '.join(against)}." if against else ""
    limit_line = _artifact_limit_instruction(phase)
    # A gating reviewer's verification artifact is reassembled from Quill's own prior findings, so
    # it answers in the delta shape. An audit lane verifies too (its findings are advisory input to
    # a finalizer) but owns its file outright, so it keeps the full-array contract.
    delta = verify and phase.gates
    structured_line = ""
    if phase.structured_findings:
        structured_line = (
            _STRUCTURED_DELTA_INSTRUCTION if delta else _STRUCTURED_FINDINGS_INSTRUCTION
        )
    prior_line = (
        _prior_findings_instruction(prior_findings, delta=delta, blocking_ids=blocking_ids)
        if verify
        else ""
    )
    owner_line = ""
    if phase.selective_on_block:
        allowed = ", ".join(phase.selective_on_block)
        owner_line = (
            f" Every OPEN CRITICAL/MAJOR finding must set owner to exactly one of: {allowed}. "
            "Choose the research lane that must rerun to resolve it. Advisory findings may omit owner."
        )
    return (
        f"TASK ({mode}): {phase.label or phase.id} for ticket {ctx.ticket} "
        f"(see TICKET block above).{against_line} "
        f"Write your findings to {_abs_artifact(ctx, findings)}."
        f"{structured_line}{owner_line}{prior_line}{limit_line}{verify_line}"
    )


def _finalizer_task(
    ctx: RunContext,
    phase: PhaseDef,
    *,
    verify: bool = False,
    prior_findings: tuple[Finding, ...] = (),
    blocking_ids: frozenset[str] | None = None,
) -> str:
    paths = [_abs_artifact(ctx, p) for p in _reconciled_findings(ctx, phase)]
    mode = "VERIFICATION mode" if verify else "finalize mode"
    artifact = phase.artifact or f"{phase.id}.md"
    listed = ", ".join(paths) if paths else "(no prior findings found)"
    verify_line = f" {_VERIFY_INSTRUCTION}" if verify else ""
    limit_line = _artifact_limit_instruction(phase)
    delta = verify and phase.gates
    structured_line = ""
    if phase.structured_findings:
        structured_line = (
            _STRUCTURED_DELTA_INSTRUCTION if delta else _STRUCTURED_FINDINGS_INSTRUCTION
        )
    prior_line = (
        _prior_findings_instruction(prior_findings, delta=delta, blocking_ids=blocking_ids)
        if verify
        else ""
    )
    reconciliation_line = (
        (
            " Adjudicate every prior blocker listed below by id, and report any genuinely new "
            "defect in new_findings. Mark a blocker RESOLVED only when current evidence proves "
            "its required outcome."
            if verify
            else (
                " Preserve every input finding ID exactly. Include each input CRITICAL/MAJOR "
                "finding and mark it RESOLVED only when current evidence proves its required "
                "outcome."
            )
        )
        if phase.structured_findings
        else (
            " Preserve every input CRITICAL/MAJOR finding ID exactly in the final blocking list."
            if artifact == PR_REVIEW_NAME
            else ""
        )
    )
    return (
        f"TASK ({mode}): {phase.label or phase.id} for ticket {ctx.ticket}. "
        f"Reconcile these reviewers' findings files: {listed}. "
        f"Write the reconciled review to {_abs_artifact(ctx, artifact)}."
        f"{structured_line}{reconciliation_line}{prior_line}{limit_line}{verify_line}"
    )


def _prior_findings_instruction(
    findings: tuple[Finding, ...],
    *,
    delta: bool = False,
    blocking_ids: frozenset[str] | None = None,
) -> str:
    """List the blockers a verification session must adjudicate.

    Under the delta contract these IDs are *context*, not something to transcribe: the model names
    them in ``dispositions`` and Quill reassembles the artifact from its own copy. The legacy
    full-array form still asks for exact preservation, so a gate that has not moved to the delta
    shape keeps its previous behavior.

    ``blocking_ids`` are the findings that actually stop *this* round under the gate's
    :class:`~quill.findings.BlockingPolicy`. Without it the severity-only test is used, which under
    ``repeat-only`` announces late discovery as a blocker when it blocked nothing — ticket #20's
    round-2 prompt named F4/F5/F6 as "PRIOR BLOCKERS" on the round they were advisory. Telling the
    one agent whose job is deciding what blocks a false account of what blocked is its own defect,
    separate from whether those findings *should* block (they legitimately do, one round later).
    """
    blockers = [finding for finding in findings if finding.blocks]
    if not blockers:
        return ""
    if delta:
        # Only the delta path splits the list. The legacy full-array form's payload doubles as the
        # preservation contract `deterministic_gate_result` enforces, so it stays whole.
        if blocking_ids is not None:
            gating = [finding for finding in blockers if finding.id in blocking_ids]
            carried = [finding for finding in blockers if finding.id not in blocking_ids]
        else:
            gating, carried = blockers, []

        def _listed(rows: list[Finding]) -> str:
            return "; ".join(
                f"{finding.id} ({finding.severity}): {finding.title} — required: "
                f"{finding.required_outcome}"
                for finding in rows
            )

        parts = []
        if gating:
            parts.append(
                f" PRIOR BLOCKERS you must adjudicate (Quill holds their full records): "
                f"{_listed(gating)}."
            )
        if carried:
            parts.append(
                " ALSO CARRIED — raised in an earlier round and not blocking this one, but they "
                f"block the next round if still unresolved: {_listed(carried)}."
            )
        return "".join(parts) + " Reference each one by id in dispositions."
    payload = json.dumps(
        [asdict(finding) for finding in blockers], ensure_ascii=False, separators=(",", ":")
    )
    return (
        " PRIOR BLOCKERS (authoritative collision-normalized JSON; do not infer them from memory): "
        f"{payload}. Copy each id, severity, title, requirement, failure_scenario, and "
        "required_outcome exactly into the new findings artifact. Update only status and evidence "
        "when current repository evidence proves resolution. A revision-suffixed ID means a later "
        "audit reused an existing ID for a different defect; preserve both findings independently."
    )


def _artifact_limit_instruction(phase: PhaseDef) -> str:
    """Compact task suffix for a configured artifact handoff ceiling."""
    if phase.max_artifact_chars is None:
        return ""
    return (
        f" Keep it under {phase.max_artifact_chars:,} characters; preserve decisions and actionable "
        "evidence, not narration, repeated context, or resolved history."
    )


def _reconciled_findings(ctx: RunContext, phase: PhaseDef) -> list[str]:
    """Resolve a finalizer's ``reconciles`` ids to the findings filenames those phases wrote."""
    out: list[str] = []
    for target_id in phase.reconciles:
        target = ctx.config.phase(target_id)
        if target is None:
            continue
        if target.audits:
            out.extend(_audit_findings_name(target, audit) for audit in target.audits)
            continue
        for model in target.models:
            out.append(_findings_name(target, model))
    return out


def _load_reconciled_findings(ctx: RunContext, phase: PhaseDef) -> tuple[Finding, ...]:
    """Load every structured finalizer input so blocking findings cannot be silently dropped."""
    findings: list[Finding] = []
    for artifact in _reconciled_findings(ctx, phase):
        path = ctx.artifact_path(artifact)
        try:
            findings.extend(load_findings(path))
        except ValueError as exc:
            raise ValueError(f"invalid reconciled findings '{artifact}': {exc}") from exc
    return tuple(findings)


def _findings_name(phase: PhaseDef, model: str) -> str:
    """Per-model findings filename: ``review-<phaseid>-<slug>.md`` (#33 decision 5)."""
    slug = slugify(model) or "model"
    return f"review-{phase.id}-{slug}.md"


def _audit_findings_name(phase: PhaseDef, audit: AuditDef) -> str:
    """Stable findings path for one named lane, independent of its shared model."""
    return f"review-{phase.id}-{slugify(audit.id)}.md"


def _against_artifacts(ctx: RunContext, phase: PhaseDef) -> list[str]:
    """Resolve a reviewer's ``against`` ids to the producer artifact filenames it reviews against.

    Config validation guarantees each target exists and has an artifact, so this never guesses a
    name. Empty when the phase declares no ``against`` — the persona's own pointer then stands.
    """
    out: list[str] = []
    for target_id in phase.against:
        target = ctx.config.phase(target_id)
        if target is not None and target.artifact:
            out.append(target.artifact)
    return out


# -- helpers ----------------------------------------------------------------------


def _verdict_of(result: PhaseResult) -> str:
    return str(result.outcome.value)


def _halt(ctx: RunContext, *, reason: str, phase: str) -> Event:
    ev = events.run_halted(reason=reason, phase=phase)
    ctx.on_event(ev)
    return ev


def _fail(ctx: RunContext, *, reason: str, phase: str) -> Event:
    ev = events.run_failed(reason=reason, phase=phase)
    ctx.on_event(ev)
    return ev
