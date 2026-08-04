"""Shared authoritative REST/SSE projections for live run state."""

from __future__ import annotations

from collections.abc import Callable

from quill_api.schemas import ModelLoadInfo, PhaseGraph, QueueView, RunSummary
from quill.phase_graph import route_counts
from quill_api.state import RunState, RunStore


def run_summary(run: RunState, position: Callable[[str], int | None]) -> RunSummary:
    phase_durations: dict[str, float] = {}
    for entry in run.history:
        if entry.duration_s is not None:
            phase_durations[entry.phase] = phase_durations.get(entry.phase, 0.0) + entry.duration_s
    return RunSummary(
        run_id=run.run_id,
        status=run.status.value,
        repo=run.repo,
        branch=run.branch,
        ticket=run.ticket,
        mode=run.mode,
        workflow=run.workflow,
        pr_number=run.pr_number,
        pr_head_sha=run.pr_head_sha,
        feedback_digest=run.feedback_digest,
        source_run_id=run.source_run_id,
        start_phase=run.start_phase,
        clear_prefix_cache=run.clear_prefix_cache,
        phase=run.phase,
        phase_label=run.phase_label,
        phase_started_at=run.phase_started_at,
        active_phases=dict(run.active_phases),
        self_checks=dict(run.self_checks),
        self_fixes=dict(run.self_fixes),
        activity=run.activity,
        activity_label=run.activity_label,
        attempt=run.attempt,
        max_attempts=run.max_attempts,
        pr_url=run.pr_url,
        question=run.question,
        error=run.error,
        failure_code=run.failure_code,
        failure_label=run.failure_label,
        queued_at=run.queued_at,
        started_at=run.started_at,
        updated_at=run.updated_at,
        live_usage=run.live_usage,
        phase_graph=PhaseGraph.model_validate(run.phase_graph)
        if run.phase_graph is not None
        else None,
        phase_route_counts=route_counts(run.phase_graph, run.phase_sequence),
        phase_durations=phase_durations,
        model_loads=[
            ModelLoadInfo(
                load_id=load.load_id,
                phase=load.phase,
                label=load.label,
                model=load.model,
                started_at=load.started_at,
                duration_s=load.duration_s,
                status=load.status,
                reason=load.reason,
            )
            for load in run.model_loads
        ],
        queue_position=position(run.run_id),
    )


def queue_view(store: RunStore, position: Callable[[str], int | None], depth: int) -> QueueView:
    active = store.active
    return QueueView(
        active=run_summary(active, position) if active else None,
        queued=[run_summary(run, position) for run in store.queued()],
        depth=depth,
    )
