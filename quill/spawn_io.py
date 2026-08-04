"""Streaming subprocess runner shared by the CLI runners.

Every phase spawns a coding-agent CLI whose ``--mode json`` / ``--format json`` output is a
JSONL event stream (the model's thinking, tool calls, text, token usage). Historically quill
buffered the whole stream and only read the final receipt, so a 10–30 min phase went completely
dark and left nothing to debug. :func:`run_streaming` instead **tees the settled events to a file
in the run dir as they arrive** — flushed per line so ``tail -f`` shows the model working live —
while still returning the full stdout for receipt extraction.

Only *settled* events are written to the file; the per-token delta events (which each re-embed
the whole message-so-far) are dropped so the transcript stays the size of the CLI's own session
store rather than growing quadratically. See :func:`_keep_in_file`.

The write is best-effort: a logging failure never breaks a run. Timeout and process-launch
errors are raised as the runner's own ``SpawnTimeout`` / ``SpawnError`` so classification is
unchanged from the old ``subprocess.run`` path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from quill.live_usage import LiveUsage
from quill.phases import SpawnError, SpawnTimeout

# ``--mode json`` / ``--format json`` streams two kinds of event: incremental *deltas* (one per
# token, each re-embedding the whole message-so-far — pi's ``message_update`` alone made a 17 KB
# plan's transcript 200 MB) and *settled* events (message/tool/turn boundaries with final content
# once). We persist only the settled events, matching the size of pi/opencode's own session store,
# while still returning the full raw stdout in memory so receipt extraction is unaffected. A line
# is dropped only if it is JSON with a droppable ``type``; anything unparsable (log noise, errors)
# is kept so nothing diagnostic is lost.
_DROP_EXACT = frozenset({"message_update"})
_DROP_SUFFIXES = ("_delta",)


def _keep_in_file(line: str) -> bool:
    """True if ``line`` should be written to the transcript file (settled event or non-JSON)."""
    stripped = line.strip()
    if not stripped:
        return False
    try:
        obj = json.loads(stripped)
    except ValueError:
        return True  # non-JSON (log noise / partial) — keep; never hide diagnostics
    if not isinstance(obj, dict):
        return True
    etype = obj.get("type")
    if not isinstance(etype, str):
        return True
    return etype not in _DROP_EXACT and not etype.endswith(_DROP_SUFFIXES)


# The event a CLI emits when it begins executing one tool call. Both pi (``--mode json``) and
# opencode (``--format json``) name it identically, so one constant covers both runners; a CLI that
# names it differently simply reports no tools rather than breaking the spawn.
_TOOL_START = "tool_execution_start"


class _PiUsageAccumulator:
    """Turn Pi's per-request partial usage into one monotonic spawn total."""

    def __init__(self) -> None:
        self._settled_output = 0
        self._parent = LiveUsage()
        self._children: dict[str, LiveUsage] = {}
        self._last_emitted = LiveUsage()

    def consume(self, line: str) -> LiveUsage | None:
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        child = pi_child_usage(obj)
        if child is not None:
            key, child_snapshot = child
            self._children[key] = child_snapshot
            return self._combined()

        event_type = obj.get("type")
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        if event_type not in ("message_update", "message_end"):
            return None
        usage = _pi_message_usage(message)
        if usage is None:
            return None
        self._parent = LiveUsage(
            usage.input_tokens,
            self._settled_output + usage.output_tokens,
            usage.total_tokens,
        )
        if event_type == "message_end":
            self._settled_output = self._parent.output_tokens
        return self._combined()

    def _combined(self) -> LiveUsage | None:
        total = LiveUsage(
            self._parent.input_tokens + sum(item.input_tokens for item in self._children.values()),
            self._parent.output_tokens
            + sum(item.output_tokens for item in self._children.values()),
            self._parent.context_window_tokens
            + sum(item.context_window_tokens for item in self._children.values()),
        )
        if total == self._last_emitted:
            return None
        self._last_emitted = total
        return total


def pi_child_usage(event: dict[str, object]) -> tuple[str, LiveUsage] | None:
    """Return the latest aggregate child usage from a pi-subagents progress/final event."""
    event_type = event.get("type")
    if event.get("toolName") != "subagent" or event_type not in {
        "tool_execution_update",
        "tool_execution_end",
    }:
        return None
    envelope = (
        event.get("partialResult") if event_type == "tool_execution_update" else event.get("result")
    )
    if not isinstance(envelope, dict):
        return None
    details = envelope.get("details")
    if not isinstance(details, dict):
        return None
    run_id = details.get("runId")
    key = (
        str(run_id)
        if isinstance(run_id, str) and run_id
        else str(event.get("toolCallId") or "subagent")
    )
    raw_total = details.get("totalChildUsage")
    usages: list[dict[str, object]] = []
    if isinstance(raw_total, dict):
        usages.append(cast(dict[str, object], raw_total))
    else:
        results = details.get("results")
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                raw_usage = item.get("usage")
                if isinstance(raw_usage, dict):
                    usages.append(cast(dict[str, object], raw_usage))
    if not usages:
        return None

    def token(raw: dict[str, object], name: str) -> int:
        value = raw.get(name, 0)
        return (
            max(0, int(value))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else 0
        )

    return key, LiveUsage(
        sum(
            token(raw, "input") + token(raw, "cacheRead") + token(raw, "cacheWrite")
            for raw in usages
        ),
        sum(token(raw, "output") for raw in usages),
        sum(
            token(raw, "input")
            + token(raw, "cacheRead")
            + token(raw, "cacheWrite")
            + token(raw, "output")
            for raw in usages
        ),
    )


def _pi_message_usage(message: dict[str, object]) -> LiveUsage | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    def token(name: str) -> int:
        value = usage.get(name, 0)
        return max(0, int(value)) if isinstance(value, (int, float)) else 0

    return LiveUsage(
        input_tokens=token("input") + token("cacheRead") + token("cacheWrite"),
        output_tokens=token("output"),
        context_window_tokens=(
            token("input") + token("cacheRead") + token("cacheWrite") + token("output")
        ),
    )


def _tool_name(line: str) -> str | None:
    """The tool name from a ``tool_execution_start`` line, or ``None`` for any other line.

    A phase's spawn is a long silence between ``phase_started`` and ``phase_done`` (impl routinely
    runs 100+ tool calls over tens of minutes). Naming each tool as it starts is what lets the
    caller show a live progress counter, so a reader can tell a working phase from a hung one
    without hand-parsing the JSONL transcript.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != _TOOL_START:
        return None
    name = obj.get("toolName")
    return name if isinstance(name, str) and name else None


def run_streaming(
    cmd: Sequence[str],
    *,
    cwd: str,
    stream_path: Path,
    agent: str,
    timeout: float,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    on_tool: Callable[[str], None] | None = None,
    on_usage: Callable[[LiveUsage], None] | None = None,
    should_stop: Callable[[], str | None] | None = None,
    abort_reason: Callable[[], str | None] | None = None,
) -> str:
    """Run ``cmd``, teeing each stdout line to ``stream_path`` live, and return full stdout.

    stdout is read line by line and appended to ``stream_path`` (flushed per line) as it arrives,
    so the file is a live JSONL transcript of the spawn. stderr is captured separately and folded
    into the raised error on a non-zero exit.

    ``on_tool`` is called with each tool's name as the worker starts it, for live progress. It runs
    on the pump thread, so it must be cheap and must not raise — an exception there would kill the
    reader and hang the spawn until its timeout, so failures are swallowed.

    Raises:
        SpawnTimeout: the process exceeded ``timeout`` (CLIs have no native timeout).
        SpawnError: the process could not be launched or exited non-zero.
    """
    usage_accumulator = _PiUsageAccumulator()
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line-buffered
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise SpawnError(f"could not launch {agent!r}: {exc}") from exc

    # Feed the prompt on stdin from a thread so a large prompt can't deadlock against a stdout
    # pipe that fills before we've started reading it.
    if input_text is not None and proc.stdin is not None:
        writer = threading.Thread(target=_feed_stdin, args=(proc, input_text), daemon=True)
        writer.start()

    out_lines: list[str] = []

    def _pump() -> None:
        """Read stdout line by line, teeing the *settled* events to the stream file (flushed).
        Always accumulate the full raw stdout for receipt extraction; only the file is filtered.
        Runs in a thread so the caller can enforce the timeout even if the process emits nothing
        (a silent hang)."""
        try:
            sink = stream_path.open("w", encoding="utf-8")
        except OSError:
            sink = None  # best-effort; still consume stdout for the receipt
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out_lines.append(line)
                if on_usage is not None:
                    usage = usage_accumulator.consume(line)
                    if usage is not None:
                        try:
                            on_usage(usage)
                        except Exception:  # noqa: BLE001,S110 - observers cannot break execution
                            pass
                if on_tool is not None:
                    name = _tool_name(line)
                    if name is not None:
                        try:
                            on_tool(name)
                        except Exception:  # noqa: BLE001,S110 - observers cannot break execution
                            pass  # progress display must never break a run
                if sink is not None and _keep_in_file(line):
                    try:
                        sink.write(line)
                        sink.flush()
                    except OSError:
                        sink = None  # stop writing, keep consuming
        finally:
            if sink is not None:
                try:
                    sink.close()
                except OSError:
                    pass

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    # Enforce the timeout against the whole spawn: a process that hangs without ever writing to
    # stdout would block the read loop forever, so we can't rely on proc.wait(timeout) alone.
    deadline = time.monotonic() + timeout
    stopped_reason: str | None = None
    while pump.is_alive():
        stopped_reason = should_stop() if should_stop is not None else None
        stopped_reason = stopped_reason or (abort_reason() if abort_reason is not None else None)
        if stopped_reason is not None:
            _terminate_process_tree(proc)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_tree(proc)
            pump.join(5)  # let the reader drain the closed pipe
            raise SpawnTimeout(f"{agent!r} spawn exceeded {timeout:g}s")
        pump.join(min(0.1, remaining))

    if stopped_reason is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
        pump.join(5)  # let the reader drain the closed pipe
        raise SpawnError(f"{agent!r} spawn aborted: {stopped_reason}")

    returncode = proc.wait()
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    stdout = "".join(out_lines)
    if returncode != 0:
        raise SpawnError(f"{agent!r} spawn exited {returncode}: {stderr.strip()[:500]}")
    return stdout


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Ask the worker and every subprocess it launched to terminate."""
    if os.name == "nt":
        proc.terminate()
    else:
        os.killpg(proc.pid, signal.SIGTERM)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Force-kill the worker process tree after graceful termination times out."""
    if os.name == "nt":
        proc.kill()
    else:
        os.killpg(proc.pid, signal.SIGKILL)


def _feed_stdin(proc: subprocess.Popen[str], text: str) -> None:
    """Write ``text`` to the process stdin and close it (best-effort, runs in a thread)."""
    try:
        if proc.stdin is not None:
            proc.stdin.write(text)
            proc.stdin.close()
    except OSError:
        pass
