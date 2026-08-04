"""Restart lineage preserves evidence without allowing stale or unsafe files."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from quill.eventlog import EventLog
from quill.restart import model_overrides, seed_events, seed_transcripts, write_seed
from quill.telemetry import build_breakdown
from quill_api.projections import run_summary
from quill_api.state import RunState, RunStatus


def _execution(
    sequence: int,
    phase: str,
    *,
    started_at: float,
    finished_at: float,
    transcript: str,
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "phase": phase,
        "label": phase.title(),
        "call_number": 1,
        "phase_type": "producer",
        "model": "model-35b",
        "verdict": "DONE",
        "rejection_reason": None,
        "self_check_status": "passed" if phase == "plan" else "not_run",
        "self_check_duration_s": 0.25 if phase == "plan" else None,
        "self_fix_status": "not_run",
        "self_fix_duration_s": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": finished_at - started_at,
        "tool_calls_by_name": {"read": sequence},
        "transcripts": [transcript],
    }


def _stream(path: Path, *, session_id: str, timestamp: float, input_tokens: int) -> None:
    path.write_text(
        "\n".join(
            (
                json.dumps({"type": "session", "id": session_id}),
                json.dumps(
                    {
                        "type": "message_end",
                        "timestamp": timestamp,
                        "message": {
                            "timestamp": timestamp,
                            "model": "model-35b",
                            "provider": "vllm",
                            "usage": {
                                "input": input_tokens,
                                "output": 10,
                                "totalTokens": input_tokens + 10,
                                "cost": {"total": 0},
                            },
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_restart_lineage_replays_graph_history_and_usage(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    source_plan = {
        "type": "run_plan",
        "ts": 0.5,
        "phase_graph": {
            "nodes": [
                {"id": "plan", "label": "Plan", "type": "producer", "order": 0},
                {"id": "test", "label": "Test", "type": "mechanical", "order": 1},
            ],
            "edges": [
                {
                    "key": "plan->test",
                    "source": "plan",
                    "target": "test",
                    "kinds": ["normal"],
                }
            ],
        },
    }
    (source / "state.jsonl").write_text(json.dumps(source_plan) + "\n", encoding="utf-8")
    executions = [
        _execution(
            1,
            "plan",
            started_at=1.0,
            finished_at=2.0,
            transcript="stream-plan-model-1.jsonl",
        ),
        _execution(
            2,
            "test",
            started_at=3.0,
            finished_at=4.0,
            transcript="stream-test-model-1.jsonl",
        ),
    ]
    _stream(
        source / "stream-plan-model-1.jsonl",
        session_id="plan-session",
        timestamp=1.5,
        input_tokens=100,
    )
    _stream(
        source / "stream-test-model-1.jsonl",
        session_id="test-session",
        timestamp=3.5,
        input_tokens=200,
    )

    inherited = seed_events("source", source, executions)
    write_seed(
        target,
        source_run_id="source",
        source_sequence=3,
        phase="build",
        start_phase="build",
        executions=executions,
    )
    for name in seed_transcripts(target):
        shutil.copy2(source / name, target / name)
    with EventLog(target) as event_log:
        for event in inherited:
            event_log.append(event)

    state = RunState(run_id="target", ticket=1, status=RunStatus.QUEUED)
    for event in inherited:
        state.fold_event(event)
    state.status = RunStatus.QUEUED
    state.active_phases.clear()
    summary = run_summary(state, lambda _run_id: None)
    breakdown = build_breakdown("target", target, {"status": "queued", "backend": "vllm"})

    assert [item["phase"] for item in breakdown["phase_executions"]] == ["plan", "test"]
    assert breakdown["cumulative_usage"]["total_tokens"] == 320
    assert breakdown["phase_executions"][0]["self_check_status"] == "passed"
    assert summary.phase_route_counts == {"plan->test": 1}


def test_restart_models_prefer_observed_execution_and_seed_paths_are_safe(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "state.jsonl").write_text(
        json.dumps(
            {
                "type": "run_queued",
                "model_overrides": {"plan": "model-27b", "later": "model-35b"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    executions = [
        {
            "phase": "plan",
            "model": "model-35b",
            "transcripts": ["stream-plan-model-1.jsonl", "../outside.jsonl"],
        }
    ]

    assert model_overrides(source, executions) == {
        "plan": "model-35b",
        "later": "model-35b",
    }
    write_seed(
        target,
        source_run_id="source",
        source_sequence=2,
        phase="test",
        start_phase="test",
        executions=executions,
    )
    assert seed_transcripts(target) == {"stream-plan-model-1.jsonl"}
