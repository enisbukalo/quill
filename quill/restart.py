"""Durable lineage carried from a terminal run into a phase restart."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quill import events

SEED_NAME = "restart-lineage.json"


def model_overrides(run_dir: Path, executions: list[dict[str, Any]]) -> dict[str, str]:
    """Return the effective source-run model choices, preferring observed execution evidence."""
    inherited: dict[str, str] = {}
    path = run_dir / "state.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                raw = event.get("model_overrides")
                if event.get("type") == events.RUN_QUEUED and isinstance(raw, dict):
                    inherited.update(
                        {
                            str(phase): str(model)
                            for phase, model in raw.items()
                            if isinstance(phase, str) and isinstance(model, str)
                        }
                    )

    observed: dict[str, set[str]] = {}
    for execution in executions:
        phase = execution.get("phase")
        model = execution.get("model")
        if not isinstance(phase, str) or not isinstance(model, str) or "+" in model:
            continue
        configured_phase = phase.split(".", 1)[0]
        observed.setdefault(configured_phase, set()).add(model)
    for phase, models in observed.items():
        if len(models) == 1:
            inherited[phase] = next(iter(models))
    return inherited


def seed_events(
    source_run_id: str,
    source_dir: Path,
    executions: list[dict[str, Any]],
) -> list[events.Event]:
    """Build a compact replay of completed source work for the new run's graph and history."""
    result: list[events.Event] = []
    plan = _latest_plan(source_dir / "state.jsonl")
    if plan is not None:
        result.append(_inherited(plan, source_run_id))

    cutoff = max(
        (
            float(value)
            for execution in executions
            if isinstance((value := execution.get("finished_at")), (int, float))
            and not isinstance(value, bool)
        ),
        default=None,
    )
    if cutoff is not None:
        result.extend(_model_load_events(source_dir / "state.jsonl", cutoff, source_run_id))

    for index, execution in enumerate(executions, 1):
        phase = str(execution["phase"])
        label = str(execution.get("label") or phase)
        finished = _number(execution.get("finished_at"), float(index))
        duration = _optional_number(execution.get("duration_s"))
        started = _number(
            execution.get("started_at"),
            finished - duration if duration is not None else finished,
        )
        started_event: events.Event = {
            "type": events.PHASE_STARTED,
            "ts": started,
            "phase": phase,
            "label": label,
            "attempt": execution.get("call_number") or 1,
            "max_attempts": execution.get("call_number") or 1,
            "phase_type": execution.get("phase_type"),
            "model": execution.get("model"),
        }
        result.append(_inherited(started_event, source_run_id))
        self_check = execution.get("self_check_status")
        if self_check in {"active", "passed", "failed"}:
            result.append(
                _inherited(
                    {
                        "type": events.SELF_CHECK_STARTED,
                        "ts": started,
                        "phase": phase,
                        "label": label,
                    },
                    source_run_id,
                )
            )
            if self_check != "active":
                result.append(
                    _inherited(
                        {
                            "type": events.SELF_CHECK_DONE,
                            "ts": finished,
                            "phase": phase,
                            "label": label,
                            "verdict": "PASS" if self_check == "passed" else "BLOCK",
                            "duration_s": execution.get("self_check_duration_s") or 0.0,
                        },
                        source_run_id,
                    )
                )
        self_fix = execution.get("self_fix_status")
        if self_fix in {"active", "completed", "failed"}:
            result.append(
                _inherited(
                    {
                        "type": events.SELF_FIX_STARTED,
                        "ts": started,
                        "phase": phase,
                        "label": label,
                    },
                    source_run_id,
                )
            )
            if self_fix != "active":
                result.append(
                    _inherited(
                        {
                            "type": events.SELF_FIX_DONE,
                            "ts": finished,
                            "phase": phase,
                            "label": label,
                            "repaired": self_fix == "completed",
                            "duration_s": execution.get("self_fix_duration_s") or 0.0,
                        },
                        source_run_id,
                    )
                )
        verdict = execution.get("verdict")
        terminal_type = events.GATE_VERDICT if verdict in {"PASS", "BLOCK"} else events.PHASE_DONE
        result.append(
            _inherited(
                {
                    "type": terminal_type,
                    "ts": finished,
                    "phase": phase,
                    "label": label,
                    "verdict": verdict,
                    "model": execution.get("model"),
                    "duration_s": duration,
                    "tools": execution.get("tool_calls_by_name"),
                    "reason": execution.get("rejection_reason"),
                },
                source_run_id,
            )
        )
    return result


def write_seed(
    target_dir: Path,
    *,
    source_run_id: str,
    source_sequence: int,
    phase: str,
    start_phase: str,
    executions: list[dict[str, Any]],
) -> None:
    """Persist the exact transcript subset and selection used by artifact inheritance."""
    transcripts = sorted(
        {
            str(name)
            for execution in executions
            for name in execution.get("transcripts", [])
            if isinstance(name, str) and name.startswith("stream-")
        }
    )
    payload = {
        "version": 1,
        "source_run_id": source_run_id,
        "source_sequence": source_sequence,
        "phase": phase,
        "start_phase": start_phase,
        "transcripts": transcripts,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / SEED_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def seed_transcripts(target_dir: Path) -> set[str]:
    """Read the allowlisted source transcripts for a prepared restart."""
    try:
        raw = json.loads((target_dir / SEED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    names = raw.get("transcripts") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        return set()
    return {
        name
        for name in names
        if isinstance(name, str) and name.startswith("stream-") and "/" not in name
    }


def _latest_plan(path: Path) -> events.Event | None:
    latest: events.Event | None = None
    if not path.is_file():
        return None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == events.RUN_PLAN:
                latest = event
    return latest


def _model_load_events(path: Path, cutoff: float, source_run_id: str) -> list[events.Event]:
    result: list[events.Event] = []
    if not path.is_file():
        return result
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") not in {
                events.MODEL_LOADING,
                events.MODEL_LOAD_DONE,
            }:
                continue
            timestamp = event.get("ts")
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                if float(timestamp) <= cutoff:
                    result.append(_inherited(event, source_run_id))
    return result


def _inherited(event: events.Event, source_run_id: str) -> events.Event:
    copied = {key: value for key, value in event.items() if value is not None}
    copied["inherited_from"] = source_run_id
    return copied


def _number(value: object, default: float) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _optional_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
