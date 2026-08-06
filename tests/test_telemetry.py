from __future__ import annotations

import json
from pathlib import Path

import pytest

from quill.telemetry import (
    build_breakdown,
    cumulative_live_usage,
    latest_usage,
    phase_window_usage,
    token_cost,
)


def test_phase_window_usage_reconciles_cards_with_displayed_phase_tokens() -> None:
    usage = phase_window_usage(
        [
            {
                "context_tokens": 500_000,
                "output_tokens": 30_000,
                "total_tokens": 530_000,
                "context_window_tokens": 150_817,
            },
            {
                "context_tokens": 600_000,
                "output_tokens": 25_000,
                "total_tokens": 625_000,
                "context_window_tokens": 171_109,
            },
            {
                "context_tokens": 450_000,
                "output_tokens": 20_000,
                "total_tokens": 470_000,
                "context_window_tokens": 138_481,
            },
            {
                "context_tokens": 325_264,
                "output_tokens": 10_587,
                "total_tokens": 335_851,
                "context_window_tokens": 96_340,
            },
        ]
    )

    assert usage["context_tokens"] == 471_160
    assert usage["output_tokens"] == 85_587
    assert usage["total_tokens"] == 556_747
    assert usage["context_window_tokens"] == 556_747
    assert usage["context_tokens"] + usage["output_tokens"] == usage["total_tokens"]


def test_build_breakdown_returns_compact_ordered_phase_statistics(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    events = [
        {"type": "session"},
        {
            "type": "message_end",
            "message": {
                "model": "m",
                "provider": "vllm",
                "timestamp": 1_700_000_001_000,
                "usage": {
                    "input": 10,
                    "output": 4,
                    "reasoning": 2,
                    "cacheRead": 3,
                    "cacheWrite": 1,
                    "totalTokens": 20,
                    "cost": {"total": 0.25},
                },
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": "make test"},
            "timestamp": 1_700_000_002_000,
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "all passed"}]},
            "isError": False,
            "timestamp": 1_700_000_003_500,
        },
        {"type": "turn_end", "timestamp": 1_700_000_004_000},
    ]
    path = run_dir / "stream-impl-m-1.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    result = build_breakdown(
        "r1",
        run_dir,
        {
            "status": "done",
            "history": [
                {
                    "phase": "impl",
                    "label": "implement",
                    "attempt": 1,
                    "ts": 1_700_000_005.0,
                    "phase_type": "producer",
                    "model": "m",
                    "duration_s": 4.0,
                    "tools": {"bash": 1},
                    "verdict": "DONE",
                    "reason": None,
                }
            ],
        },
    )

    assert result["schema_version"] == 15
    assert list(result) == [
        "status",
        "run_id",
        "phase_executions",
        "model_loads",
        "model_load_duration_s",
        "cumulative_usage",
        "completeness",
        "schema_version",
    ]
    execution = result["phase_executions"][0]
    assert execution == {
        "sequence": 1,
        "phase": "impl",
        "label": "implement",
        "call_number": 1,
        "is_retry": False,
        "phase_type": "producer",
        "model": "m",
        "verdict": "DONE",
        "rejection_reason": None,
        "self_check_status": "not_run",
        "self_check_duration_s": None,
        "self_fix_status": "not_run",
        "self_fix_duration_s": None,
        "started_at": None,
        "finished_at": None,
        "duration_s": 4.0,
        "context_tokens": 10,
        "output_tokens": 4,
        "reasoning_tokens": 2,
        "cache_read_tokens": 3,
        "cache_write_tokens": 1,
        "total_tokens": 20,
        "context_window_tokens": 20,
        "cost": pytest.approx(20 / 1_000_000 * 0.043),
        "tool_calls_total": 1,
        "tool_calls_by_name": {"bash": 1},
        "transcripts": ["stream-impl-m-1.jsonl"],
        "contract_kind": None,
        "contract_version": None,
        "contract_status": None,
        "contract_digest": None,
    }
    assert result["cumulative_usage"] == {
        "context_tokens": 16,
        "output_tokens": 4,
        "reasoning_tokens": 2,
        "cache_read_tokens": 3,
        "cache_write_tokens": 1,
        "total_tokens": 20,
        "context_window_tokens": 20,
        "cost": pytest.approx(20 / 1_000_000 * 0.043),
    }
    assert "tool_calls" not in execution
    assert "transcripts" not in result


def test_malformed_lines_are_reported_not_fatal(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "stream-plan-m-1.jsonl").write_text("noise\n{}\n", encoding="utf-8")
    result = build_breakdown("r", run_dir, {"status": "failed"})
    assert result["phase_executions"] == []
    assert result["legacy_session_observations"][0]["phase"] == "plan"
    assert "call_number" not in result["legacy_session_observations"][0]
    assert any("malformed JSON" in warning for warning in result["completeness"]["warnings"])


def test_phase_executions_preserve_revise_verify_order(tmp_path: Path) -> None:
    history = [
        {"phase": "plan", "label": "plan", "verdict": "DONE", "tools": {"read": 2}},
        {
            "phase": "review_plan",
            "label": "review plan",
            "verdict": "BLOCK",
            "reason": "missing rollback steps",
            "tools": {"read": 1},
        },
        {"phase": "plan", "label": "plan", "verdict": "DONE", "tools": {"edit": 1}},
        {
            "phase": "review_plan",
            "label": "review plan",
            "verdict": "PASS",
            "reason": None,
            "tools": {"read": 1},
        },
    ]

    result = build_breakdown("r", tmp_path / "missing", {"history": history})

    assert [item["phase"] for item in result["phase_executions"]] == [
        "plan",
        "review_plan",
        "plan",
        "review_plan",
    ]
    assert [item["call_number"] for item in result["phase_executions"]] == [1, 1, 2, 2]
    assert result["phase_executions"][1]["rejection_reason"] == "missing rollback steps"
    assert result["phase_executions"][2]["is_retry"] is True


def test_durable_event_history_is_authoritative_and_preserves_active_phase(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    durable = [
        {
            "type": "phase_started",
            "ts": 1.0,
            "phase": "plan",
            "label": "write plan",
            "attempt": 1,
            "phase_type": "producer",
            "model": "m",
        },
        {
            "type": "phase_done",
            "ts": 2.0,
            "phase": "plan",
            "label": "write plan",
            "verdict": "DONE",
            "duration_s": 1.0,
            "tools": {"read": 2},
        },
        {
            "type": "phase_started",
            "ts": 3.0,
            "phase": "review_plan",
            "label": "review plan",
            "attempt": 1,
            "phase_type": "reviewer",
            "model": "m",
        },
    ]
    (run_dir / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in durable) + "\n{truncated",
        encoding="utf-8",
    )

    result = build_breakdown(
        "r",
        run_dir,
        {
            # Deliberately contradictory: persisted state must win over stale process memory.
            "history": [{"phase": "wrong", "label": "wrong", "verdict": "DONE"}]
        },
    )

    assert result["schema_version"] == 15
    assert [item["phase"] for item in result["phase_executions"]] == ["plan", "review_plan"]
    assert result["phase_executions"][0]["tool_calls_by_name"] == {"read": 2}
    assert result["phase_executions"][1]["verdict"] is None
    assert result["phase_executions"][1]["phase_type"] == "reviewer"
    assert any("state.jsonl:4" in warning for warning in result["completeness"]["warnings"])


def test_breakdown_reports_completed_model_loads_separately_and_ignores_legacy_unmatched(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    durable = [
        {
            "type": "phase_started",
            "ts": 1.0,
            "phase": "plan",
            "label": "write plan",
            "attempt": 1,
            "phase_type": "producer",
            "model": "qwen",
        },
        {"type": "model_loading", "ts": 2.0, "phase": "plan", "model": "qwen"},
        {
            "type": "model_load_done",
            "ts": 32.0,
            "phase": "plan",
            "model": "qwen",
            "duration_s": 30.0,
            "success": True,
        },
        {"type": "phase_executing", "ts": 32.0, "phase": "plan", "model": "qwen"},
        {
            "type": "phase_done",
            "ts": 42.0,
            "phase": "plan",
            "label": "write plan",
            "duration_s": 10.0,
            "verdict": "DONE",
        },
        # Pre-v12 runs emitted this start without a matching completion event. A terminal run
        # cannot still be loading it, so the projection must not invent an active operation.
        {"type": "model_loading", "ts": 43.0, "phase": "review", "model": "gemma"},
    ]
    (run_dir / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in durable), encoding="utf-8"
    )

    result = build_breakdown("r", run_dir, {"status": "done"})

    assert result["model_load_duration_s"] == 30.0
    assert result["model_loads"] == [
        {
            "load_id": "model-load-1",
            "phase": "plan",
            "label": "plan",
            "model": "qwen",
            "started_at": 2.0,
            "finished_at": 32.0,
            "duration_s": 30.0,
            "status": "completed",
            "reason": None,
        }
    ]
    assert result["phase_executions"][0]["duration_s"] == 10.0


def test_usage_is_final_snapshot_not_sum_of_growing_context(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    events = [
        {
            "type": "message_end",
            "message": {"model": "m", "usage": {"input": 9_162, "totalTokens": 9_401}},
        },
        {
            "type": "message_end",
            "message": {
                "model": "m",
                "usage": {"input": 86_866, "output": 146, "totalTokens": 87_012},
            },
        },
        {"type": "message_end", "message": {"role": "tool", "usage": {"input": 0}}},
    ]
    (run_dir / "stream-plan-m-1.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    result = build_breakdown(
        "r",
        run_dir,
        {"history": [{"phase": "plan", "model": "m", "verdict": "DONE"}]},
    )

    execution = result["phase_executions"][0]
    assert execution["context_tokens"] == 86_866
    assert execution["total_tokens"] == 87_012
    assert result["cumulative_usage"]["context_tokens"] == 86_866


def test_same_pi_session_continuation_does_not_double_count_context_window(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    durable = [
        {"type": "phase_started", "phase": "review", "ts": 100.0, "model": "m"},
        {"type": "phase_done", "phase": "review", "ts": 140.0, "model": "m"},
    ]
    (run_dir / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in durable), encoding="utf-8"
    )
    for sequence, timestamp, tokens in ((1, 110.0, 10), (2, 130.0, 20)):
        events = [
            {"type": "session", "id": "same-pi-session"},
            {
                "type": "message_end",
                "timestamp": timestamp,
                "message": {"model": "m", "usage": {"input": tokens, "totalTokens": tokens}},
            },
        ]
        (run_dir / f"stream-review-m-{sequence}.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events), encoding="utf-8"
        )

    result = build_breakdown("r", run_dir, {"status": "done"})

    assert result["phase_executions"][0]["context_tokens"] == 30
    assert result["phase_executions"][0]["context_window_tokens"] == 20
    assert result["cumulative_usage"]["total_tokens"] == 20
    assert result["completeness"] == {"complete": True, "warnings": []}


def test_active_phase_attaches_transcript_before_first_timestamp_is_flushed(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "state.jsonl").write_text(
        json.dumps(
            {
                "type": "phase_started",
                "phase": "plan",
                "ts": 100.0,
                "model": "m",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "stream-plan-m-1.jsonl").write_text(
        json.dumps({"type": "agent_start"}), encoding="utf-8"
    )

    result = build_breakdown("r", run_dir, {"status": "running"})

    assert len(result["phase_executions"]) == 1
    assert result["phase_executions"][0]["phase"] == "plan"
    assert result["completeness"] == {"complete": True, "warnings": []}


def test_breakdown_reports_durable_self_check_status_and_duration(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    durable = [
        {"type": "phase_started", "phase": "plan", "ts": 100.0, "model": "m"},
        {"type": "self_check_started", "phase": "plan", "ts": 110.0},
        {
            "type": "self_check_done",
            "phase": "plan",
            "ts": 114.0,
            "verdict": "DONE",
            "duration_s": 4.0,
        },
        {"type": "phase_done", "phase": "plan", "ts": 115.0, "verdict": "DONE"},
    ]
    (run_dir / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in durable), encoding="utf-8"
    )

    result = build_breakdown("r", run_dir, {"status": "done"})

    execution = result["phase_executions"][0]
    assert execution["self_check_status"] == "passed"
    assert execution["self_check_duration_s"] == 4.0


def test_breakdown_reports_latest_self_fix_result_and_cumulative_duration(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    durable = [
        {"type": "phase_started", "phase": "plan", "ts": 100.0, "model": "m"},
        {"type": "self_fix_started", "phase": "plan", "ts": 105.0},
        {
            "type": "self_fix_done",
            "phase": "plan",
            "ts": 107.0,
            "repaired": False,
            "duration_s": 2.0,
        },
        {"type": "self_fix_started", "phase": "plan", "ts": 108.0},
        {
            "type": "self_fix_done",
            "phase": "plan",
            "ts": 111.0,
            "repaired": True,
            "duration_s": 3.0,
        },
        {"type": "phase_done", "phase": "plan", "ts": 112.0, "verdict": "DONE"},
    ]
    (run_dir / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in durable), encoding="utf-8"
    )

    execution = build_breakdown("r", run_dir, {"status": "done"})["phase_executions"][0]

    assert execution["self_fix_status"] == "completed"
    assert execution["self_fix_duration_s"] == 5.0


# -- token cost pricing -----------------------------------------------------------


def test_token_cost_prices_local_backends_by_electricity_and_keeps_hosted_cli_cost() -> None:
    # A self-hosted backend has no API bill — price 2M tokens at $0.043/1M, ignoring the CLI figure.
    assert token_cost(2_000_000, 999.0, backend="vllm", usd_per_1m=0.043) == pytest.approx(0.086)
    assert token_cost(1_000_000, 0.0, backend="llamacpp", usd_per_1m=0.05) == pytest.approx(0.05)
    # Hosted or unknown backend reports a real cost — trust the CLI's figure.
    assert token_cost(1_000_000, 3.50, backend="anthropic", usd_per_1m=0.043) == 3.50
    assert token_cost(1_000_000, 3.50, backend=None, usd_per_1m=0.043) == 3.50


def test_latest_usage_keeps_latest_context_and_accumulates_all_output(tmp_path: Path) -> None:
    stream = tmp_path / "stream-impl-m-1.jsonl"
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"usage": {"input": 5, "output": 1, "totalTokens": 6}},
                    }
                ),
                json.dumps({"type": "tool_execution_start", "toolName": "read"}),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"usage": {"input": 40, "output": 12, "totalTokens": 52}},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    usage = latest_usage(stream)
    assert usage["input_tokens"] == 40
    assert usage["output_tokens"] == 13
    assert usage["total_tokens"] == 53


def test_latest_usage_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert latest_usage(tmp_path / "nope.jsonl")["total_tokens"] == 0


def test_latest_usage_includes_each_subagent_run_latest_snapshot(tmp_path: Path) -> None:
    stream = tmp_path / "stream-impl-1.jsonl"
    stream.write_text(
        "\n".join(
            [
                '{"type":"message_end","message":{"role":"assistant","usage":{"input":100,"output":2,"totalTokens":102}}}',
                '{"type":"tool_execution_update","toolName":"subagent","toolCallId":"one","partialResult":{"details":{"runId":"children","results":[{"usage":{"input":40,"cacheRead":10,"output":5}}]}}}',
                '{"type":"tool_execution_end","toolName":"subagent","toolCallId":"one","result":{"details":{"runId":"children","totalChildUsage":{"input":90,"cacheRead":10,"output":12}}}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert latest_usage(stream) == {
        "input_tokens": 200,
        "output_tokens": 14,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 214,
        "context_window_tokens": 214,
        "cost": 0.0,
    }


def test_build_breakdown_prices_a_local_run_from_tokens_not_the_cli_cost(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "stream-impl-m-1.jsonl").write_text(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "model": "m",
                    "timestamp": 1_700_000_000_000,
                    "usage": {
                        "input": 1_000_000,
                        "output": 0,
                        "totalTokens": 1_000_000,
                        "cost": {"total": 99.0},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    run = {"backend": "vllm", "history": [{"phase": "impl", "label": "implement"}]}
    breakdown = build_breakdown("r", run_dir, run, usd_per_1m=0.043)
    # The CLI claimed $99 for a local model; the run is priced from tokens: 1M * $0.043 = $0.043.
    assert breakdown["cumulative_usage"]["cost"] == pytest.approx(0.043)


def test_cumulative_live_usage_sums_in_out_across_all_phase_transcripts(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "stream-plan-m-1.jsonl").write_text(
        json.dumps({"type": "message_end", "message": {"usage": {"input": 100, "output": 20}}}),
        encoding="utf-8",
    )
    (run_dir / "stream-impl-m-1.jsonl").write_text(
        json.dumps({"type": "message_end", "message": {"usage": {"input": 900, "output": 80}}}),
        encoding="utf-8",
    )
    usage = cumulative_live_usage(run_dir)
    assert usage["input_tokens"] == 1000  # 100 + 900, phase-agnostic
    assert usage["output_tokens"] == 100  # 20 + 80
    assert usage["total_tokens"] == 1100  # total = in + out
