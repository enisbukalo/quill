"""Streaming subprocess helper tests: tees stdout to the run-dir file, live and flushed."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from quill.phases import SpawnError, SpawnTimeout
from quill.live_usage import LiveUsage
from quill.spawn_io import run_streaming


def test_streams_settled_events_to_file_and_returns_full_stdout(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    # Emit two settled events with a per-token delta between them (the delta must be dropped from
    # the file but kept in the returned stdout).
    lines = [
        '{"type": "message_start"}',
        '{"type": "message_update", "partial": "huge repeated payload"}',
        '{"type": "message_end", "text": "DONE: ok"}',
    ]
    prog = "import sys\n" + "\n".join(f"print({line!r})" for line in lines)
    out = run_streaming(
        [sys.executable, "-c", prog],
        cwd=str(tmp_path),
        stream_path=stream,
        agent="test",
        timeout=30,
    )
    # Full raw stdout is returned unfiltered (receipt extraction still sees everything).
    assert out == "\n".join(lines) + "\n"
    # The file keeps the settled events but drops the delta.
    file_text = stream.read_text(encoding="utf-8")
    assert '"type": "message_start"' in file_text
    assert '"type": "message_end"' in file_text
    assert "message_update" not in file_text
    assert "huge repeated payload" not in file_text


def test_file_keeps_non_json_noise(tmp_path: Path) -> None:
    """Unparsable lines (log noise, errors) are kept — never hide diagnostics."""
    stream = tmp_path / "stream.jsonl"
    prog = 'print(\'loading model...\')\nprint(\'{"type": "thinking_delta", "d": "x"}\')'
    run_streaming(
        [sys.executable, "-c", prog],
        cwd=str(tmp_path),
        stream_path=stream,
        agent="test",
        timeout=30,
    )
    file_text = stream.read_text(encoding="utf-8")
    assert "loading model..." in file_text  # non-JSON kept
    assert "thinking_delta" not in file_text  # *_delta dropped


def test_feeds_prompt_on_stdin(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    prog = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    out = run_streaming(
        [sys.executable, "-c", prog],
        cwd=str(tmp_path),
        stream_path=stream,
        agent="test",
        timeout=30,
        input_text="hello",
    )
    assert out == "HELLO"
    # "HELLO" is non-JSON, so it is kept in the transcript.
    assert stream.read_text(encoding="utf-8") == "HELLO"


def test_nonzero_exit_raises_spawn_error(tmp_path: Path) -> None:
    prog = "import sys; sys.stderr.write('kaboom'); sys.exit(3)"
    with pytest.raises(SpawnError, match="exited 3"):
        run_streaming(
            [sys.executable, "-c", prog],
            cwd=str(tmp_path),
            stream_path=tmp_path / "s.jsonl",
            agent="test",
            timeout=30,
        )


def test_timeout_raises_spawn_timeout(tmp_path: Path) -> None:
    prog = "import time; time.sleep(30)"
    with pytest.raises(SpawnTimeout, match="exceeded"):
        run_streaming(
            [sys.executable, "-c", prog],
            cwd=str(tmp_path),
            stream_path=tmp_path / "s.jsonl",
            agent="test",
            timeout=1,
        )


def test_stop_terminates_active_spawn_immediately(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    prog = "import time; time.sleep(30)"
    with pytest.raises(SpawnError, match="stopped by request"):
        run_streaming(
            [sys.executable, "-c", prog],
            cwd=str(tmp_path),
            stream_path=tmp_path / "s.jsonl",
            agent="test",
            timeout=30,
            should_stop=lambda: "stopped by request" if stop.is_set() else None,
        )


def test_phase_abort_terminates_only_spawn_with_reason(tmp_path: Path) -> None:
    prog = "import time; time.sleep(30)"
    with pytest.raises(SpawnError, match="external phase guard"):
        run_streaming(
            [sys.executable, "-c", prog],
            cwd=str(tmp_path),
            stream_path=tmp_path / "s.jsonl",
            agent="test",
            timeout=30,
            abort_reason=lambda: "external phase guard requested abort",
        )


def test_missing_binary_raises_spawn_error(tmp_path: Path) -> None:
    with pytest.raises(SpawnError, match="could not launch"):
        run_streaming(
            ["definitely-not-a-real-binary-xyz"],
            cwd=str(tmp_path),
            stream_path=tmp_path / "s.jsonl",
            agent="test",
            timeout=5,
        )


def test_pi_usage_stream_accumulates_requests_and_tools_live(tmp_path: Path) -> None:
    stream = tmp_path / "usage.jsonl"
    lines = [
        '{"type":"message_update","message":{"role":"assistant","usage":{"input":90,"cacheRead":10,"cacheWrite":0,"output":1}}}',
        '{"type":"message_update","message":{"role":"assistant","usage":{"input":90,"cacheRead":10,"cacheWrite":0,"output":2}}}',
        '{"type":"message_end","message":{"role":"assistant","usage":{"input":90,"cacheRead":10,"cacheWrite":0,"output":2}}}',
        '{"type":"tool_execution_start","toolName":"read"}',
        '{"type":"message_update","message":{"role":"assistant","usage":{"input":140,"cacheRead":10,"cacheWrite":0,"output":1}}}',
        '{"type":"message_end","message":{"role":"assistant","usage":{"input":140,"cacheRead":10,"cacheWrite":0,"output":1}}}',
    ]
    prog = "\n".join(f"print({line!r})" for line in lines)
    usage: list[LiveUsage] = []
    tools: list[str] = []

    run_streaming(
        [sys.executable, "-c", prog],
        cwd=str(tmp_path),
        stream_path=stream,
        agent="pi:test",
        timeout=30,
        on_usage=usage.append,
        on_tool=tools.append,
    )

    assert usage == [
        LiveUsage(100, 1, 101),
        LiveUsage(100, 2, 102),
        LiveUsage(150, 3, 151),
    ]
    assert tools == ["read"]
    assert "message_update" not in stream.read_text(encoding="utf-8")


def test_pi_usage_ignores_malformed_negative_and_non_assistant_values(tmp_path: Path) -> None:
    lines = [
        "not json",
        '{"type":"message_update","message":{"role":"user","usage":{"input":99}}}',
        '{"type":"message_update","message":{"role":"assistant","usage":{"input":-5,"output":"x"}}}',
    ]
    prog = "\n".join(f"print({line!r})" for line in lines)
    usage: list[LiveUsage] = []
    run_streaming(
        [sys.executable, "-c", prog],
        cwd=str(tmp_path),
        stream_path=tmp_path / "malformed.jsonl",
        agent="pi:test",
        timeout=30,
        on_usage=usage.append,
    )
    assert usage == []


def test_pi_usage_combines_live_subagent_snapshots_without_double_counting(tmp_path: Path) -> None:
    lines = [
        '{"type":"message_end","message":{"role":"assistant","usage":{"input":100,"output":2}}}',
        '{"type":"tool_execution_update","toolName":"subagent","toolCallId":"call-1","partialResult":{"details":{"runId":"run-1","results":[{"usage":{"input":40,"cacheRead":10,"output":5}}]}}}',
        '{"type":"tool_execution_update","toolName":"subagent","toolCallId":"call-1","partialResult":{"details":{"runId":"run-1","results":[{"usage":{"input":40,"cacheRead":10,"output":5}}]}}}',
        '{"type":"tool_execution_update","toolName":"subagent","toolCallId":"call-1","partialResult":{"details":{"runId":"run-1","results":[{"usage":{"input":40,"cacheRead":10,"output":5}},{"usage":{"input":55,"cacheRead":5,"output":7}}]}}}',
        '{"type":"tool_execution_end","toolName":"subagent","toolCallId":"call-1","result":{"details":{"runId":"run-1","totalChildUsage":{"input":95,"cacheRead":15,"output":12}}}}',
        '{"type":"message_end","message":{"role":"assistant","usage":{"input":150,"output":1}}}',
    ]
    prog = "\n".join(f"print({line!r})" for line in lines)
    usage: list[LiveUsage] = []

    run_streaming(
        [sys.executable, "-c", prog],
        cwd=str(tmp_path),
        stream_path=tmp_path / "nested.jsonl",
        agent="pi:test",
        timeout=30,
        on_usage=usage.append,
    )

    assert usage == [
        LiveUsage(100, 2, 102),
        LiveUsage(150, 7, 157),
        LiveUsage(210, 14, 224),
        LiveUsage(260, 15, 273),
    ]
