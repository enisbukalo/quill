"""Run lifecycle: submit, observe, stop, decide, and read artifacts."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse

from quill import events
from quill.checkpoints import CheckpointManifest, load_manifest
from quill.eventlog import EventLog
from quill.git_ops import GitError, SubprocessRunner
from quill.pipeline import make_run_id
from quill.preflight import gh_authenticated, gh_available
from quill.telemetry import SCHEMA_VERSION, build_breakdown
from quill.restart import model_overrides as restart_model_overrides
from quill.restart import seed_events, write_seed
from quill_api.deps import ServicesDep
from quill_api.paths import PathEscape, resolve_within
from quill_api.queue import QueuedRun
from quill_api.schemas import (
    ArtifactContent,
    ArtifactInfo,
    ArtifactList,
    DecisionRequest,
    DeleteRunsRequest,
    DeleteRunsResult,
    ModelLoadInfo,
    PhaseHistoryEntry,
    PhaseGraph,
    QueueView,
    RestartOptions,
    RestartPhaseChoice,
    RestartRunRequest,
    RunDetail,
    RunList,
    RunSummary,
    StartRunRequest,
)
from quill_api.services import Services
from quill_api.projections import queue_view, run_summary
from quill_api.state import RunState, RunStatus
from quill_api.workspace import WorkspaceError, validate_branch, validate_repo

router = APIRouter(tags=["runs"])

_HISTORICAL_REPO_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True, slots=True)
class _RestartCandidate:
    phase: str
    start_phase: str
    sequence: int
    call_number: int
    label: str
    verdict: str | None
    model: str | None
    checkpoint: str


def _history_repo(value: str | None) -> str:
    """Normalize URL-shaped rows written by pre-MCP server versions."""
    if not value:
        return ""
    match = _HISTORICAL_REPO_RE.match(value)
    return match.group(1) if match else value


def _summary(services: Services, run: RunState) -> RunSummary:
    return run_summary(run, services.queue.position)


def _terminal_breakdown(run_id: str, services: Services) -> dict[str, Any]:
    """Build current-schema telemetry for restart selection, bypassing stale cached projections."""
    cached = services.history.get_breakdown(run_id)
    if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
        return cached
    live = services.store.get(run_id)
    row = services.history.get(run_id)
    if live is not None:
        payload: dict[str, object] = _summary(services, live).model_dump()
        payload["backend"] = live.backend
        payload["history"] = [
            PhaseHistoryEntry(
                phase=item.phase,
                label=item.label,
                verdict=item.verdict,
                attempt=item.attempt,
                ts=item.ts,
                phase_type=item.phase_type,
                model=item.model,
                duration_s=item.duration_s,
                tools=item.tools,
                reason=item.reason,
            ).model_dump()
            for item in live.history
        ]
    else:
        assert row is not None
        payload = {
            "status": row.status,
            "repo": _history_repo(row.repo),
            "branch": row.branch,
            "ticket": row.ticket,
            "mode": row.mode,
            "workflow": row.workflow,
            "source_run_id": row.source_run_id,
            "start_phase": row.start_phase,
            "started_at": row.started_at or None,
            "updated_at": row.finished_at or row.started_at,
        }
    return build_breakdown(
        run_id,
        services.settings.runs_root / run_id,
        payload,
        usd_per_1m=services.settings.usd_per_1m_tokens,
    )


def _checkpoint_times(
    services: Services, repo: str, manifest: CheckpointManifest
) -> dict[str, float]:
    """Resolve old manifest timestamps from their private Git checkpoint commits."""
    result: dict[str, float] = {}
    checkpoints = manifest.checkpoints
    try:
        runner = SubprocessRunner(str(services.workspaces.path_for(repo)))
    except WorkspaceError:
        runner = None
    for checkpoint in checkpoints:
        created_at = getattr(checkpoint, "created_at", None)
        if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
            result[checkpoint.commit] = float(created_at)
            continue
        if runner is None:
            continue
        try:
            raw = runner(["git", "show", "-s", "--format=%ct", checkpoint.commit]).strip()
            result[checkpoint.commit] = float(raw)
        except (GitError, ValueError):
            continue
    return result


def _start_phase(phase: str, graph: PhaseGraph | None) -> str:
    """Map an observable audit lane back to the configured phase the engine can enter."""
    configured = {node.id for node in graph.nodes} if graph is not None else set()
    if phase in configured:
        return phase
    parent = phase.split(".", 1)[0]
    return parent if parent in configured else phase


def _restart_candidates(
    run_id: str,
    services: Services,
    *,
    repo: str,
    manifest: CheckpointManifest,
) -> tuple[list[_RestartCandidate], dict[str, Any]]:
    """Match real execution rows to the checkpoint captured immediately before each row."""
    breakdown = _terminal_breakdown(run_id, services)
    executions = [
        item
        for item in breakdown.get("phase_executions", [])
        if isinstance(item, dict)
        and isinstance(item.get("phase"), str)
        and isinstance(item.get("sequence"), int)
        and isinstance(item.get("call_number"), int)
    ]
    graph, _, _, _, _ = _historical_graph(services.settings.runs_root / run_id)
    times = _checkpoint_times(services, repo, manifest)
    used_sequences: set[int] = set()
    candidates: list[_RestartCandidate] = []
    for checkpoint in manifest.checkpoints:
        exact = [item for item in executions if item["phase"] == checkpoint.phase]
        audit = [
            item for item in executions if str(item["phase"]).startswith(f"{checkpoint.phase}.")
        ]
        matching = exact or audit
        if not matching:
            continue
        timestamp = times.get(checkpoint.commit)
        available = [item for item in matching if int(item["sequence"]) not in used_sequences]
        if not available:
            continue
        if timestamp is None:
            selected = available[0]
        else:
            selected = min(
                available,
                key=lambda item: abs(float(item.get("started_at") or 0.0) - timestamp),
            )
        # One parent checkpoint is the shared boundary for every concurrent audit lane. Expose an
        # action on each row, but make all of them re-enter the configured parent phase.
        selected_rows = (
            [item for item in audit if item.get("call_number") == selected.get("call_number")]
            if audit
            else [selected]
        )
        for item in selected_rows:
            sequence = int(item["sequence"])
            if sequence in used_sequences:
                continue
            used_sequences.add(sequence)
            phase = str(item["phase"])
            candidates.append(
                _RestartCandidate(
                    phase=phase,
                    start_phase=checkpoint.phase if audit else _start_phase(phase, graph),
                    sequence=sequence,
                    call_number=int(item["call_number"]),
                    label=str(item.get("label") or phase),
                    verdict=str(item["verdict"]) if item.get("verdict") is not None else None,
                    model=str(item["model"]) if item.get("model") is not None else None,
                    checkpoint=checkpoint.commit,
                )
            )
    candidates.sort(key=lambda item: item.sequence)
    return candidates, breakdown


def _restart_options(run_id: str, services: Services) -> RestartOptions:
    """Compute restart eligibility from durable run metadata and current Git state."""
    live = services.store.get(run_id)
    row = services.history.get(run_id)
    if live is None and row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id}")
    assert live is not None or row is not None
    if live is None:
        assert row is not None
    if live is not None:
        run_status = live.status.value
        mode = live.mode
        repo = live.repo
        branch = live.branch
        pr_url = live.pr_url
        pr_number = live.pr_number
    else:
        assert row is not None
        run_status = row.status
        mode = row.mode
        repo = _history_repo(row.repo)
        branch = row.branch
        pr_url = row.pr_url
        pr_number = row.pr_number
    if run_status not in {"failed", "halted"}:
        return RestartOptions(eligible=False, reason="only failed or halted runs can restart")
    if mode != "create":
        return RestartOptions(eligible=False, reason="only new-ticket runs can restart by phase")
    if not repo or not branch:
        return RestartOptions(eligible=False, reason="run has no recoverable repository branch")
    if pr_url or pr_number:
        return RestartOptions(eligible=False, reason="run is already associated with a PR")
    active = services.store.active
    if active is not None or services.queue.depth:
        active_id = active.run_id if active is not None else "queued run"
        return RestartOptions(
            eligible=False,
            reason=f"restart availability can be checked after {active_id} finishes",
        )
    manifest = load_manifest(services.settings.runs_root / run_id)
    if manifest is None or manifest.repo != repo or manifest.branch != branch:
        return RestartOptions(eligible=False, reason="run has no valid phase checkpoints")
    try:
        branch_status = services.workspaces.restart_status(
            repo, branch, base="main", checkpoint_base=manifest.base
        )
    except WorkspaceError as exc:
        return RestartOptions(eligible=False, reason=str(exc))
    if not branch_status.eligible:
        return RestartOptions(eligible=False, reason=branch_status.reason)
    candidates, _ = _restart_candidates(run_id, services, repo=repo, manifest=manifest)
    phases = [
        RestartPhaseChoice(
            id=item.phase,
            label=item.label,
            sequence=item.sequence,
            call_number=item.call_number,
            start_phase=item.start_phase,
            verdict=item.verdict,
            model=item.model,
        )
        for item in candidates
    ]
    if not phases:
        return RestartOptions(eligible=False, reason="run has no restorable phase boundary")
    return RestartOptions(eligible=True, phases=phases)


def _historical_graph(
    run_dir: Path,
) -> tuple[PhaseGraph | None, dict[str, int], dict[str, float], str | None, str | None]:
    """Recover declared topology, or observed topology for pre-contract durable runs."""
    path = run_dir / "state.jsonl"
    if not path.is_file():
        return None, {}, {}, None, None
    graph: PhaseGraph | None = None
    sequence: list[str] = []
    observed_nodes: dict[str, tuple[str, str]] = {}
    phase_durations: dict[str, float] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            if event.get("type") == events.RUN_PLAN and event.get("phase_graph") is not None:
                graph = PhaseGraph.model_validate(event["phase_graph"])
            elif event.get("type") == events.PHASE_STARTED and isinstance(event.get("phase"), str):
                phase = event["phase"]
                sequence.append(phase)
                label = event.get("label")
                phase_type = event.get("phase_type")
                observed_nodes.setdefault(
                    phase,
                    (
                        label if isinstance(label, str) else phase,
                        phase_type if isinstance(phase_type, str) else "phase",
                    ),
                )
            if event.get("type") in {events.PHASE_DONE, events.GATE_VERDICT}:
                phase = event.get("phase")
                duration = event.get("duration_s")
                if (
                    isinstance(phase, str)
                    and isinstance(duration, int | float)
                    and not isinstance(duration, bool)
                ):
                    phase_durations[phase] = phase_durations.get(phase, 0.0) + float(duration)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, {}, {}, None, None
    if graph is None:
        nodes = [
            {"id": phase, "label": label, "type": phase_type, "order": order}
            for order, (phase, (label, phase_type)) in enumerate(observed_nodes.items())
        ]
        order_by_phase = {phase: order for order, phase in enumerate(observed_nodes)}
        observed_edges: dict[tuple[str, str], str] = {}
        for source, target in zip(sequence, sequence[1:], strict=False):
            if source == target:
                continue
            kind = "retry" if order_by_phase[target] <= order_by_phase[source] else "normal"
            observed_edges.setdefault((source, target), kind)
        graph = PhaseGraph.model_validate(
            {
                "nodes": nodes,
                "edges": [
                    {
                        "key": f"{source}->{target}",
                        "source": source,
                        "target": target,
                        "kinds": [kind],
                    }
                    for (source, target), kind in observed_edges.items()
                ],
            }
        )
    counts = {edge.key: 0 for edge in graph.edges}
    edge_keys = {(edge.source, edge.target): edge.key for edge in graph.edges}
    for source, target in zip(sequence, sequence[1:], strict=False):
        key = edge_keys.get((source, target))
        if key is not None:
            counts[key] += 1
    last_phase = sequence[-1] if sequence else None
    last_label = observed_nodes.get(last_phase, (last_phase, "phase"))[0] if last_phase else None
    return graph, counts, phase_durations, last_phase, last_label


def _historical_model_loads(run_dir: Path) -> list[ModelLoadInfo]:
    """Recover actual model switches and their outcomes from the durable event stream."""
    path = run_dir / "state.jsonl"
    if not path.is_file():
        return []
    loads: list[ModelLoadInfo] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        phase = event.get("phase")
        model = event.get("model")
        if etype == events.MODEL_LOADING and isinstance(phase, str) and isinstance(model, str):
            started_at = event.get("ts")
            if not isinstance(started_at, int | float) or isinstance(started_at, bool):
                continue
            loads.append(
                ModelLoadInfo(
                    load_id=f"model-load-{len(loads) + 1}",
                    phase=phase,
                    label=str(event.get("label") or phase),
                    model=model,
                    started_at=float(started_at),
                    status="active",
                )
            )
        elif etype == events.MODEL_LOAD_DONE and isinstance(phase, str) and isinstance(model, str):
            for load in reversed(loads):
                if load.status != "active" or load.phase != phase or load.model != model:
                    continue
                duration = event.get("duration_s")
                load.duration_s = (
                    float(duration)
                    if isinstance(duration, int | float) and not isinstance(duration, bool)
                    else None
                )
                load.status = "completed" if event.get("success") is True else "failed"
                reason = event.get("reason")
                load.reason = reason if isinstance(reason, str) else None
                break
    # Historical runs are terminal. An unmatched pre-v12 ``model_loading`` event has no durable
    # completion timing and must not appear as a permanently active graph node.
    return [load for load in loads if load.status != "active"]


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def start_run(body: StartRunRequest, services: ServicesDep) -> RunSummary:
    """Start the sole GPU-owning run, rejecting any second active submission."""
    if not (gh_available() and gh_authenticated()):
        # 424 Failed Dependency: the run cannot reach GitHub, so there is no point queueing it.
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="GitHub CLI (gh) is not installed or not authenticated; run `gh auth login`.",
        )
    try:
        repo = validate_repo(body.repo)
        branch = validate_branch(body.branch)
    except WorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if body.mode == "create" and body.generated_branch and not branch.endswith(f"_{body.ticket}"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"create branch must end with _{body.ticket}",
        )
    try:
        state = services.admit_run(
            repo=repo,
            branch=branch,
            ticket=body.ticket,
            mode=body.mode,
            workflow=body.workflow,
            clear_prefix_cache=body.clear_prefix_cache,
            model_overrides=tuple(sorted(body.model_overrides.items())),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _summary(services, state)


@router.get("/runs/{run_id}/restart-options")
def restart_options(run_id: str, services: ServicesDep) -> RestartOptions:
    """List phase boundaries that remain safe to restore for one terminal run."""
    return _restart_options(run_id, services)


@router.post("/runs/{run_id}/restart", status_code=status.HTTP_202_ACCEPTED)
def restart_run(run_id: str, body: RestartRunRequest, services: ServicesDep) -> RunSummary:
    """Create a linked run from one exact, observable phase execution boundary."""
    options = _restart_options(run_id, services)
    if not options.eligible:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=options.reason)
    matching_options = [
        item
        for item in options.phases
        if item.id == body.phase and (body.sequence is None or item.sequence == body.sequence)
    ]
    if not matching_options:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"phase execution '{body.phase}' has no restorable checkpoint",
        )
    # Legacy API/MCP callers sent only a phase. Preserve that surface by choosing its latest
    # restorable execution; the frontend always sends the exact row sequence.
    selected_option = max(matching_options, key=lambda item: item.sequence)
    source = services.store.get(run_id)
    row = services.history.get(run_id)
    assert source is not None or row is not None
    if source is None:
        assert row is not None
    if source is not None:
        repo = source.repo
        branch = source.branch
        ticket = source.ticket
        workflow = source.workflow
    else:
        assert row is not None
        repo = _history_repo(row.repo)
        branch = row.branch
        ticket = row.ticket
        workflow = row.workflow
    assert branch is not None
    manifest = load_manifest(services.settings.runs_root / run_id)
    assert manifest is not None
    candidates, source_breakdown = _restart_candidates(
        run_id, services, repo=repo, manifest=manifest
    )
    selected = next(
        item
        for item in candidates
        if item.phase == selected_option.id and item.sequence == selected_option.sequence
    )
    source_executions = [
        item
        for item in source_breakdown.get("phase_executions", [])
        if isinstance(item, dict) and int(item.get("sequence") or 0) < selected.sequence
    ]
    inherited_models = restart_model_overrides(
        services.settings.runs_root / run_id,
        [item for item in source_breakdown.get("phase_executions", []) if isinstance(item, dict)],
    )
    inherited_models.update(body.model_overrides)

    with services.run_admission_lock:
        existing = services.store.active
        if existing is not None or services.queue.depth:
            active_id = existing.run_id if existing is not None else "queued run"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Quill already has an active run ({active_id}); wait for it to finish.",
            )
        new_run_id = make_run_id(ticket)
        base_run_id = new_run_id
        suffix = 2
        while services.store.get(new_run_id) is not None or services.history.get(new_run_id):
            new_run_id = f"{base_run_id}-{suffix}"
            suffix += 1
        state = RunState(
            run_id=new_run_id,
            ticket=ticket,
            repo=repo,
            branch=branch,
            mode="create",
            workflow=workflow,
            source_run_id=run_id,
            start_phase=selected.start_phase,
        )
        target_dir = services.settings.runs_root / new_run_id
        inherited_events = seed_events(
            run_id,
            services.settings.runs_root / run_id,
            source_executions,
        )
        for event in inherited_events:
            state.fold_event(event)
        # The replay above intentionally retains history, graph topology, completed routes,
        # self-checks, and model-load statistics. Only lifecycle/current-work fields are new.
        state.status = RunStatus.QUEUED
        state.phase = None
        state.phase_label = None
        state.phase_started_at = None
        state.active_phases.clear()
        state.activity = "queued"
        state.activity_label = "Queued"
        state.started_at = None
        state.model = None
        state.error = None
        write_seed(
            target_dir,
            source_run_id=run_id,
            source_sequence=selected.sequence,
            phase=selected.phase,
            start_phase=selected.start_phase,
            executions=source_executions,
        )
        services.store.add(state)
        services.history.record(state)
        services.project_queue.attach_run(state)
        with EventLog(target_dir) as event_log:
            for event in inherited_events:
                event_log.append(event)
            event_log.append(
                events.run_queued(
                    new_run_id,
                    ticket,
                    repo=repo,
                    branch=branch,
                    mode="create",
                    workflow=workflow,
                    model_overrides=dict(sorted(inherited_models.items())) or None,
                    source_run_id=run_id,
                    start_phase=selected.start_phase,
                )
            )
        services.queue.submit(
            QueuedRun(
                run_id=new_run_id,
                repo=repo,
                branch=branch,
                ticket=ticket,
                mode="create",
                workflow=workflow,
                model_overrides=tuple(sorted(inherited_models.items())),
                source_run_id=run_id,
                start_phase=selected.start_phase,
                checkpoint_commit=selected.checkpoint,
            )
        )
    return _summary(services, state)


@router.get("/runs")
def list_runs(
    services: ServicesDep,
    repo: Annotated[str | None, Query(description="Filter to one owner/name")] = None,
    ticket: Annotated[int | None, Query(gt=0)] = None,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunList:
    """Live runs first, then history from SQLite."""
    memory = services.store.all()
    memory_ids = {r.run_id for r in memory}
    summaries = [
        _summary(services, r)
        for r in reversed(memory)
        if (repo is None or r.repo == repo)
        and (ticket is None or r.ticket == ticket)
        and (run_status is None or r.status.value == run_status)
    ]

    for row in services.history.recent(
        limit=offset + limit + len(memory) + 1,
        repo=repo,
        ticket=ticket,
        run_status=run_status,
    ):
        if row.run_id in memory_ids:
            continue
        phase = row.last_phase
        phase_label = row.last_phase_label
        if phase is None:
            _, _, _, phase, phase_label = _historical_graph(
                services.settings.runs_root / row.run_id
            )
        summaries.append(
            RunSummary(
                run_id=row.run_id,
                status=row.status,
                repo=_history_repo(row.repo),
                branch=row.branch,
                ticket=row.ticket,
                mode=row.mode,
                workflow=row.workflow,
                pr_number=row.pr_number,
                pr_head_sha=row.pr_head_sha,
                feedback_digest=row.feedback_digest,
                source_run_id=row.source_run_id,
                start_phase=row.start_phase,
                clear_prefix_cache=row.clear_prefix_cache,
                pr_url=row.pr_url,
                error=row.error,
                failure_code=row.failure_code,
                failure_label=row.failure_label,
                phase=phase,
                phase_label=phase_label,
                queued_at=row.started_at,
                started_at=row.started_at or None,
                updated_at=row.finished_at or row.started_at,
            )
        )
    page = summaries[offset : offset + limit]
    return RunList(
        runs=page,
        limit=limit,
        offset=offset,
        has_more=len(summaries) > offset + limit,
    )


@router.delete("/runs")
def delete_runs(body: DeleteRunsRequest, services: ServicesDep) -> DeleteRunsResult:
    """Delete terminal runs, their cached projections, and their artifact directories."""
    run_ids = list(dict.fromkeys(body.run_ids))
    missing = [
        run_id
        for run_id in run_ids
        if services.store.get(run_id) is None and services.history.get(run_id) is None
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown runs: {', '.join(missing)}",
        )
    active = [
        run_id
        for run_id in run_ids
        if (run := services.store.get(run_id)) is not None and run.is_active
    ]
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"active runs cannot be deleted: {', '.join(active)}",
        )

    for run_id in run_ids:
        try:
            run_dir = resolve_within(services.settings.runs_root, run_id)
            manifest = load_manifest(run_dir)
            if manifest is not None:
                services.workspaces.delete_run_checkpoint_ref(manifest.repo, run_id)
            if run_dir.is_dir():
                shutil.rmtree(run_dir)
        except (OSError, PathEscape, WorkspaceError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"could not delete artifacts for {run_id}: {exc}",
            ) from exc
    services.history.delete_many(run_ids)
    for run_id in run_ids:
        services.store.discard(run_id)
    return DeleteRunsResult(deleted=run_ids)


@router.get("/queue")
def read_queue(services: ServicesDep) -> QueueView:
    """What is running and what is waiting."""
    return queue_view(services.store, services.queue.position, services.queue.depth)


@router.get("/runs/{run_id}")
def read_run(run_id: str, services: ServicesDep) -> RunDetail:
    run = services.store.get(run_id)
    if run is not None:
        summary = _summary(services, run)
        return RunDetail(
            **summary.model_dump(),
            history=[
                PhaseHistoryEntry(
                    phase=h.phase,
                    label=h.label,
                    verdict=h.verdict,
                    attempt=h.attempt,
                    ts=h.ts,
                    phase_type=h.phase_type,
                    model=h.model,
                    duration_s=h.duration_s,
                    tools=h.tools,
                    reason=h.reason,
                )
                for h in run.history
            ],
        )

    row = services.history.get(run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id}")
    run_dir = services.settings.runs_root / run_id
    phase_graph, phase_route_counts, phase_durations, last_phase, last_label = _historical_graph(
        run_dir
    )
    return RunDetail(
        run_id=row.run_id,
        status=row.status,
        repo=_history_repo(row.repo),
        branch=row.branch,
        ticket=row.ticket,
        mode=row.mode,
        workflow=row.workflow,
        pr_number=row.pr_number,
        pr_head_sha=row.pr_head_sha,
        feedback_digest=row.feedback_digest,
        source_run_id=row.source_run_id,
        start_phase=row.start_phase,
        clear_prefix_cache=row.clear_prefix_cache,
        pr_url=row.pr_url,
        error=row.error,
        failure_code=row.failure_code,
        failure_label=row.failure_label,
        phase=last_phase,
        phase_label=last_label,
        queued_at=row.started_at,
        started_at=row.started_at or None,
        updated_at=row.finished_at or row.started_at,
        phase_graph=phase_graph,
        phase_route_counts=phase_route_counts,
        phase_durations=phase_durations,
        model_loads=_historical_model_loads(run_dir),
    )


@router.get("/runs/{run_id}/breakdown")
def read_breakdown(run_id: str, services: ServicesDep) -> dict[str, object]:
    """Complete persisted telemetry, including every normalized tool call and result."""
    state = services.store.get(run_id)
    row = services.history.get(run_id)
    run_dir = services.settings.runs_root / run_id
    if state is None and row is None and not run_dir.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id}")
    if state is None:
        cached = services.history.get_breakdown(run_id)
        if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
            return cached
        assert row is not None
        run: dict[str, object] = {
            "status": row.status,
            "repo": _history_repo(row.repo),
            "branch": row.branch,
            "ticket": row.ticket,
            "mode": row.mode,
            "workflow": row.workflow,
            "pr_number": row.pr_number,
            "pr_head_sha": row.pr_head_sha,
            "feedback_digest": row.feedback_digest,
            "source_run_id": row.source_run_id,
            "start_phase": row.start_phase,
            "queued_at": row.started_at,
            "started_at": row.started_at or None,
            "updated_at": row.finished_at or row.started_at,
            "finished_at": row.finished_at or None,
            "pr_url": row.pr_url,
            "error": row.error,
        }
    else:
        run = _summary(services, state).model_dump()
        run["backend"] = state.backend
        run["history"] = [
            PhaseHistoryEntry(
                phase=h.phase,
                label=h.label,
                verdict=h.verdict,
                attempt=h.attempt,
                ts=h.ts,
                phase_type=h.phase_type,
                model=h.model,
                duration_s=h.duration_s,
                tools=h.tools,
                reason=h.reason,
            ).model_dump()
            for h in state.history
        ]
    breakdown = build_breakdown(
        run_id, run_dir, run, usd_per_1m=services.settings.usd_per_1m_tokens
    )
    services.history.record_breakdown(run_id, breakdown, SCHEMA_VERSION)
    return breakdown


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str, services: ServicesDep) -> RunSummary:
    """Stop a run immediately, or remove it from the queue before it starts."""
    run = services.store.get(run_id)
    if run is not None and run.status.value == "queued" and services.queue.cancel(run_id):
        services.manager.cancel_queued(run_id)
        return _summary(services, run)
    if not services.manager.request_stop(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id}")
    run = services.store.get(run_id)
    assert run is not None  # request_stop already established it exists
    return _summary(services, run)


@router.post("/runs/{run_id}/decision")
def decide(run_id: str, body: DecisionRequest, services: ServicesDep) -> RunSummary:
    """Answer a parked needs-decision so the run can continue."""
    if not services.manager.answer_decision(run_id, body.answer):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run {run_id} is not waiting on a decision",
        )
    run = services.store.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id}")
    return _summary(services, run)


@router.get("/runs/{run_id}/artifacts")
def list_artifacts(run_id: str, services: ServicesDep) -> ArtifactList:
    """The files a run produced — plans, findings, logs, transcripts."""
    run_dir = services.settings.runs_root / run_id
    if not run_dir.is_dir():
        return ArtifactList(run_id=run_id, artifacts=[])
    return ArtifactList(
        run_id=run_id,
        artifacts=[
            ArtifactInfo(name=path.name, size=path.stat().st_size)
            for path in sorted(run_dir.iterdir())
            if path.is_file()
        ],
    )


@router.get("/runs/{run_id}/artifacts.zip")
def download_artifacts(run_id: str, services: ServicesDep) -> StreamingResponse:
    """Download every artifact from one run as a ZIP archive."""
    run_dir = services.settings.runs_root / run_id
    artifacts = (
        sorted(path for path in run_dir.iterdir() if path.is_file()) if run_dir.is_dir() else []
    )
    if not artifacts:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no artifacts")
    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in artifacts:
            bundle.write(path, arcname=path.name)
    archive.seek(0)
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)

    def chunks() -> Iterator[bytes]:
        try:
            while data := archive.read(64 * 1024):
                yield data
        finally:
            archive.close()

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_run_id}-artifacts.zip"'},
    )


@router.get("/runs/{run_id}/artifact-downloads/{name:path}")
def download_artifact(run_id: str, name: str, services: ServicesDep) -> FileResponse:
    """Download one artifact without loading it into the browser UI."""
    run_dir = services.settings.runs_root / run_id
    try:
        target = resolve_within(run_dir, name)
    except PathEscape as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"artifact {name}")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.get("/runs/{run_id}/artifacts/{name:path}")
def read_artifact(run_id: str, name: str, services: ServicesDep) -> ArtifactContent:
    """One artifact's text, jailed to that run's directory."""
    run_dir = services.settings.runs_root / run_id
    try:
        target = resolve_within(run_dir, name)
    except PathEscape as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"artifact {name}")
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return ArtifactContent(run_id=run_id, name=name, content=content)
