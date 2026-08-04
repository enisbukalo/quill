"""Service introspection: health, version, models, and the /init bootstrap payload."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from quill import __version__
from quill.bootstrap import ASSETS_DIR
from quill.config import (
    CONFIG_FILENAME,
    DEFAULT_CI_SECONDS,
    DEFAULT_MODEL_LOAD_SECONDS,
    DEFAULT_OPENCODE_RUN_SECONDS,
    DEFAULT_RETRY,
    MECHANICAL_STEPS,
    PHASE_TYPES,
)
from quill.loader import ModelLoader, ModelLoadError, router_url
from quill.modelserver import VllmServer
from quill.preflight import gh_authenticated, gh_available
from quill_api import catalog
from quill_api.deps import ServicesDep
from quill_api.model_registry import SwitchInProgress
from quill_api.routers.catalog import _info
from quill_api.schemas import (
    ConfigSchema,
    HealthInfo,
    InitPayload,
    LifetimeStats,
    FailureLifetimeStats,
    LoadedModelInfo,
    ModelLifetimeStats,
    ModelSwitchRequest,
    ModelSwitchStateInfo,
    ModelsInfo,
    PhaseLifetimeStats,
    RunLifetimePoint,
    SwitchableModelInfo,
    SystemTelemetryInfo,
    TelemetryDisplaySettings,
    VersionInfo,
)

router = APIRouter(tags=["system"])

_started = time.time()


@router.get("/health")
def health(services: ServicesDep) -> HealthInfo:
    return HealthInfo(
        status="up",
        uptime_s=round(time.time() - _started, 1),
        driver_version=__version__,
        gh_available=gh_available(),
        gh_authenticated=gh_authenticated(),
        queue_depth=services.queue.depth,
    )


@router.get("/version")
def version() -> VersionInfo:
    return VersionInfo(quill=__version__, api=__version__)


@router.get("/models")
def models(services: ServicesDep, refresh: bool = False) -> ModelsInfo:
    """Report the vLLM server, everything this machine could load, and any switch in flight.

    ``refresh`` re-probes systemd. The probe costs seconds across a few hundred units, so the
    default answers from cache; the UI passes it only on an explicit user action.
    """
    with VllmServer(services.settings.vllm_url) as vllm:
        reachable = vllm.healthy()
        try:
            cards = vllm.model_cards() if reachable else []
        except ModelLoadError:
            cards = []
    details = [LoadedModelInfo.model_validate(card) for card in cards]
    resident = {model.id for model in details}
    switchable = [
        SwitchableModelInfo(
            model_id=entry.model_id,
            service=entry.service,
            unit_state=entry.unit_state,
            available=entry.available,
            unavailable_reason=entry.unavailable_reason,
            resident=entry.model_id in resident,
            max_model_len=entry.max_model_len,
            max_concurrency=entry.max_concurrency,
            max_batched_tokens=entry.max_batched_tokens,
            tensor_parallel_size=entry.tensor_parallel_size,
            quantization=entry.quantization,
            kv_cache_dtype=entry.kv_cache_dtype,
            gpu_memory_utilization=entry.gpu_memory_utilization,
        )
        for entry in services.model_registry.models(refresh=refresh)
    ]
    return ModelsInfo(
        backend="vllm",
        loaded=[model.id for model in details],
        model_details=details,
        switchable=switchable,
        switch=_switch_state(services),
        reachable=reachable,
        url=services.settings.vllm_url,
    )


def _switch_state(services: ServicesDep) -> ModelSwitchStateInfo:
    return ModelSwitchStateInfo(**asdict(services.model_switcher.state))


@router.get("/models/switch")
def model_switch_state(services: ServicesDep) -> ModelSwitchStateInfo:
    """Current switch progress, so a reconnecting client recovers state it missed."""
    return _switch_state(services)


def _guard_model_control(force: bool, services: ServicesDep, action: str) -> None:
    """Reject a model mutation that would interrupt an active or queued run."""
    if force:
        return
    busy = [run.run_id for run in services.store.all() if run.status in ("running", "queued")]
    if busy:
        raise HTTPException(
            409,
            {
                "message": (
                    f"{action} now would stop the model these runs are using; "
                    "retry with force to proceed anyway"
                ),
                "runs": busy,
            },
        )


@router.post("/models/switch", status_code=202)
def switch_model(request: ModelSwitchRequest, services: ServicesDep) -> ModelSwitchStateInfo:
    """Make a model resident, without blocking on the minutes it takes.

    Starting a model unit stops whatever was resident (each declares ``Conflicts=`` against its
    siblings), so an in-flight run would lose its model mid-phase. Active or queued runs therefore
    reject the switch unless the caller explicitly forces it — the check a standalone switcher
    cannot make, because it has no idea runs exist.
    """
    entry = services.model_registry.resolve(request.model_id)
    if entry is None:
        raise HTTPException(404, f"no vllm service on this machine serves '{request.model_id}'")
    if not entry.available:
        raise HTTPException(
            409, f"'{request.model_id}' cannot be started: {entry.unavailable_reason}"
        )
    _guard_model_control(request.force, services, "switching")
    try:
        return ModelSwitchStateInfo(
            **asdict(services.model_switcher.start(entry, forced=request.force))
        )
    except SwitchInProgress as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/models/unload", status_code=202)
def unload_model(request: ModelSwitchRequest, services: ServicesDep) -> ModelSwitchStateInfo:
    """Stop the systemd service associated with a resident vLLM model.

    The stop is idempotent at the systemd layer. Quill applies the same active-run guard used by
    model switching because unloading a run's resident model breaks the phase in flight.
    """
    entry = services.model_registry.resolve(request.model_id)
    if entry is None:
        raise HTTPException(404, f"no vllm service on this machine serves '{request.model_id}'")
    _guard_model_control(request.force, services, "unloading")
    try:
        return ModelSwitchStateInfo(
            **asdict(services.model_switcher.unload(entry, forced=request.force))
        )
    except SwitchInProgress as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/telemetry")
def telemetry(services: ServicesDep) -> SystemTelemetryInfo:
    return SystemTelemetryInfo.model_validate(services.telemetry.latest.as_dict())


@router.get("/settings/telemetry")
def telemetry_settings(services: ServicesDep) -> TelemetryDisplaySettings:
    stored = services.history.get_setting("telemetry_display")
    return TelemetryDisplaySettings.model_validate(stored or {})


@router.put("/settings/telemetry")
def update_telemetry_settings(
    body: TelemetryDisplaySettings, services: ServicesDep
) -> TelemetryDisplaySettings:
    services.history.set_setting("telemetry_display", body.model_dump())
    return body


@router.get("/stats")
def lifetime_stats(services: ServicesDep) -> LifetimeStats:
    """Lifetime outcomes and usage for runs still retained in history."""
    rows = services.history.lifetime_rows()
    status_counts: dict[str, int] = defaultdict(int)
    failure_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        status_counts[row.status] += 1
        if row.failure_code:
            failure_counts[(row.failure_code, row.failure_label or row.failure_code)] += 1
    duration_s = sum(
        max(0.0, row.finished_at - row.started_at)
        for row in rows
        if row.finished_at > 0 and row.started_at > 0
    )

    usage = {"context_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0}
    activity = {"phase_executions": 0, "tool_calls": 0, "self_checks": 0, "repeat_attempts": 0}
    model_load_count = 0
    model_load_duration_s = 0.0
    models: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {
            "calls": 0,
            "duration_s": 0.0,
            "context_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
        }
    )
    phases: dict[str, dict[str, str | int | float]] = {}
    recent_runs: list[RunLifetimePoint] = []
    for row in rows:
        breakdown = row.breakdown if isinstance(row.breakdown, dict) else {}
        cumulative = breakdown.get("cumulative_usage")
        if isinstance(cumulative, dict):
            for field in ("context_tokens", "output_tokens", "total_tokens"):
                usage[field] += _integer(cumulative.get(field))
            usage["cost"] += _number(cumulative.get("cost"))
        recent_runs.append(
            RunLifetimePoint(
                run_id=row.run_id,
                status=row.status,
                started_at=row.started_at,
                duration_s=max(0.0, row.finished_at - row.started_at),
                total_tokens=_integer(cumulative.get("total_tokens"))
                if isinstance(cumulative, dict)
                else 0,
            )
        )
        model_loads = breakdown.get("model_loads")
        if isinstance(model_loads, list):
            for model_load in model_loads:
                if not isinstance(model_load, dict) or model_load.get("status") == "active":
                    continue
                model_load_count += 1
                model_load_duration_s += _number(model_load.get("duration_s"))
        executions = breakdown.get("phase_executions")
        if not isinstance(executions, list):
            continue
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            activity["phase_executions"] += 1
            activity["tool_calls"] += _integer(execution.get("tool_calls_total"))
            phase_name = execution.get("phase")
            if isinstance(phase_name, str):
                phase = phases.setdefault(
                    phase_name,
                    {
                        "label": str(execution.get("label") or phase_name),
                        "executions": 0,
                        "duration_s": 0.0,
                        "total_tokens": 0,
                        "tool_calls": 0,
                    },
                )
                phase["executions"] = int(phase["executions"]) + 1
                phase["duration_s"] = float(phase["duration_s"]) + _number(
                    execution.get("duration_s")
                )
                phase["total_tokens"] = int(phase["total_tokens"]) + _integer(
                    execution.get("total_tokens")
                )
                phase["tool_calls"] = int(phase["tool_calls"]) + _integer(
                    execution.get("tool_calls_total")
                )
            if execution.get("self_check_status") in {"passed", "failed"}:
                activity["self_checks"] += 1
            if (
                execution.get("is_retry") is True
                or _integer(execution.get("call_number")) > 1
                or _integer(execution.get("attempt")) > 1
            ):
                activity["repeat_attempts"] += 1
            if not isinstance(execution.get("model"), str):
                continue
            model = models[execution["model"]]
            model["calls"] += 1
            model["duration_s"] += _number(execution.get("duration_s"))
            for field in ("context_tokens", "output_tokens", "total_tokens"):
                model[field] += _integer(execution.get(field))
            model["cost"] += _number(execution.get("cost"))

    model_rows = [
        ModelLifetimeStats(
            model=name,
            calls=int(values["calls"]),
            duration_s=float(values["duration_s"]),
            context_tokens=int(values["context_tokens"]),
            output_tokens=int(values["output_tokens"]),
            total_tokens=int(values["total_tokens"]),
            cost=float(values["cost"]),
        )
        for name, values in models.items()
    ]
    model_rows.sort(key=lambda item: (-item.total_tokens, item.model))
    phase_rows = [
        PhaseLifetimeStats(
            phase=name,
            label=str(values["label"]),
            executions=int(values["executions"]),
            duration_s=float(values["duration_s"]),
            total_tokens=int(values["total_tokens"]),
            tool_calls=int(values["tool_calls"]),
        )
        for name, values in phases.items()
    ]
    phase_rows.sort(key=lambda item: (-item.duration_s, item.phase))
    recent_runs.sort(key=lambda item: (item.started_at, item.run_id))
    terminal = sum(status_counts.get(name, 0) for name in ("done", "failed", "halted"))
    return LifetimeStats(
        total_runs=len(rows),
        successful_runs=status_counts.get("done", 0),
        failed_runs=status_counts.get("failed", 0),
        halted_runs=status_counts.get("halted", 0),
        other_runs=len(rows) - terminal,
        repositories=len({row.repo for row in rows if row.repo}),
        tickets=len({(row.repo, row.ticket) for row in rows}),
        duration_s=duration_s,
        context_tokens=int(usage["context_tokens"]),
        output_tokens=int(usage["output_tokens"]),
        total_tokens=int(usage["total_tokens"]),
        cost=float(usage["cost"]),
        phase_executions=activity["phase_executions"],
        tool_calls=activity["tool_calls"],
        self_checks=activity["self_checks"],
        repeat_attempts=activity["repeat_attempts"],
        model_loads=model_load_count,
        model_load_duration_s=model_load_duration_s,
        models=model_rows,
        phases=phase_rows,
        recent_runs=recent_runs[-25:],
        failures=sorted(
            (
                FailureLifetimeStats(code=code, label=label, runs=count)
                for (code, label), count in failure_counts.items()
            ),
            key=lambda item: (-item.runs, item.code),
        ),
    )


def _number(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _integer(value: object) -> int:
    return int(_number(value))


@router.get("/telemetry/events")
async def telemetry_events(request: Request, services: ServicesDep) -> EventSourceResponse:
    async def generate() -> AsyncIterator[dict[str, object]]:
        async for snapshot in services.telemetry.subscribe():
            if await request.is_disconnected():
                break
            yield {"event": "telemetry", "data": json.dumps(snapshot.as_dict())}

    return EventSourceResponse(generate())


@router.get("/init")
def init(services: ServicesDep) -> InitPayload:
    """Everything an agent needs to write a repo's config without guessing.

    The schema is generated from the live constants, so it cannot drift from what the loader
    actually accepts, and the catalogs are the real ones this server would resolve at run time — a
    config written from this payload names only personas and skills that exist here.
    """
    settings = services.settings
    starter = ASSETS_DIR / CONFIG_FILENAME
    return InitPayload(
        config_filename=CONFIG_FILENAME,
        config_schema=ConfigSchema(
            phase_types=list(PHASE_TYPES),
            mechanical_steps=list(MECHANICAL_STEPS),
            required=["runner.kind", "build.command", "build.test"],
            defaults={
                "retries": {"default": DEFAULT_RETRY, "spawn": DEFAULT_RETRY},
                "timeouts": {
                    "opencode_run_seconds": DEFAULT_OPENCODE_RUN_SECONDS,
                    "model_load_seconds": DEFAULT_MODEL_LOAD_SECONDS,
                    "ci_seconds": DEFAULT_CI_SECONDS,
                },
            },
        ),
        personas=[_info(e) for e in catalog.discover_personas(settings.personas_root)],
        skills=[_info(e) for e in catalog.discover_skills(settings.skills_root)],
        models=_router_presets() or [],
        endpoints={
            "start_run": "POST /runs",
            "watch": "GET /events",
            "telemetry": "GET /telemetry",
            "telemetry_events": "GET /telemetry/events",
            "telemetry_settings": "GET, PUT /settings/telemetry",
            "lifetime_stats": "GET /stats",
            "models": "GET /models",
            "model_switch": "GET, POST /models/switch",
            "model_unload": "POST /models/unload",
            "queue": "GET /queue",
            "project_ticket_queue": "GET /project-queue",
            "project_queue_candidates": "GET /project-queue/{owner}/{name}/candidates",
            "project_queue_submit": "POST /project-queue/{owner}/{name}",
            "personas": "GET /personas",
            "skills": "GET /skills",
            "artifacts": "GET /runs/{run_id}/artifacts",
            "artifact_download": "GET /runs/{run_id}/artifact-downloads/{name}",
            "artifact_archive": "GET /runs/{run_id}/artifacts.zip",
            "github_repositories": "GET /github/repositories",
            "github_issues": "GET /github/repositories/{owner}/{name}/issues",
        },
        starter_config=starter.read_text(encoding="utf-8") if starter.is_file() else "",
    )


def _router_presets() -> list[str] | None:
    """Preset ids the llama.cpp router offers, or None when it is unreachable.

    None and ``[]`` mean different things — "no router" versus "a router with nothing configured" —
    so the caller can distinguish a down GPU stack from an empty one.
    """
    with ModelLoader(router_url()) as loader:
        try:
            presets = loader.presets()
        except Exception:  # noqa: BLE001 - reachability probe; any failure means unreachable
            return None
    return presets or None
