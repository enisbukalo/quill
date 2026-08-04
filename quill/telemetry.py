"""Build a compact, chronological statistical record of every phase invocation."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from quill import events
from quill.config import LOCAL_BACKENDS
from quill.eventlog import EVENT_LOG_NAME
from quill.spawn_io import pi_child_usage

SCHEMA_VERSION = 13


def token_cost(
    total_tokens: float, cli_cost: float, *, backend: str | None, usd_per_1m: float
) -> float:
    """USD cost of ``total_tokens``.

    A **self-hosted** backend (llamacpp/vllm) has no API bill — its real cost is electricity, so
    price the tokens locally (``total_tokens / 1M × rate``). A hosted backend reports a genuine cost,
    so keep the agent CLI's figure. An unknown backend keeps the CLI cost rather than guess.
    """
    if backend in LOCAL_BACKENDS:
        return total_tokens / 1_000_000 * usd_per_1m
    return cli_cost


_STREAM_RE = re.compile(r"^stream-(?P<body>.+)\.jsonl$")
_SEQUENCED_STREAM_RE = re.compile(r"^stream-.+-\d+\.jsonl$")


def build_breakdown(
    run_id: str, run_dir: Path, run: dict[str, Any], *, usd_per_1m: float = 0.043
) -> dict[str, Any]:
    """Return one small ordered array: one entry for every recorded phase execution.

    ``usd_per_1m`` prices tokens for a self-hosted run (see :func:`token_cost`); the backend is read
    from the ``run`` payload the caller supplies.
    """
    warnings: list[str] = []
    sessions = [
        _parse_session(path, warnings)
        for path in sorted(run_dir.glob("stream-*.jsonl"))
        if run_dir.is_dir()
    ]
    sessions.sort(key=lambda item: (item["started_at"] is None, item["started_at"] or 0.0))
    backend = run.get("backend") if isinstance(run.get("backend"), str) else None
    providers = {session["provider"] for session in sessions if session.get("provider")}
    if backend is None and len(providers) == 1:
        observed_provider = next(iter(providers))
        backend = observed_provider if observed_provider in LOCAL_BACKENDS else None

    if any(not _SEQUENCED_STREAM_RE.match(str(item["transcript"])) for item in sessions):
        warnings.append(
            "legacy transcript names detected; retries may have overwritten earlier phase "
            "executions, so phase counts and usage are lower bounds"
        )

    persisted_history = _history_from_event_log(run_dir / EVENT_LOG_NAME, warnings)
    model_loads = _model_loads_from_event_log(
        run_dir / EVENT_LOG_NAME,
        include_active=run.get("status") in {"running", "needs_decision"},
    )
    run_options = _run_options_from_event_log(run_dir / EVENT_LOG_NAME)
    memory_history = run.pop("history", [])
    history = persisted_history or [
        item
        for item in memory_history
        if isinstance(item, dict) and isinstance(item.get("phase"), str)
    ]
    phase_executions = _from_history(history, sessions, warnings) if history else []
    legacy_observations = _from_sessions(sessions, warnings) if sessions and not history else []
    graph = run.get("phase_graph")
    graph_nodes = graph.get("nodes") if isinstance(graph, dict) else None
    self_check_phases = {
        str(node["id"])
        for node in graph_nodes or []
        if isinstance(node, dict) and node.get("self_check") is True and "id" in node
    }
    for entry in phase_executions:
        if entry["phase"] in self_check_phases and entry["self_check_status"] == "not_run":
            entry["self_check_status"] = "enabled"
    # Re-price each execution from its token total for a self-hosted run; a hosted run keeps the
    # CLI's cost. Do this before summing so cumulative cost matches the per-phase entries.
    for entry in (*phase_executions, *legacy_observations):
        entry["cost"] = token_cost(
            entry.get("total_tokens", 0),
            entry.get("cost", 0.0),
            backend=backend,
            usd_per_1m=usd_per_1m,
        )
    cumulative_usage = _sum_final_usage(phase_executions or legacy_observations)

    result: dict[str, Any] = {
        **run,
        **run_options,
        "run_id": run_id,
        "phase_executions": phase_executions,
        "model_loads": model_loads,
        "model_load_duration_s": sum(float(load.get("duration_s") or 0.0) for load in model_loads),
        "cumulative_usage": cumulative_usage,
        "completeness": {
            "complete": not warnings,
            "warnings": warnings,
        },
        "schema_version": SCHEMA_VERSION,
    }
    if legacy_observations:
        result["legacy_session_observations"] = legacy_observations
    return result


def _model_loads_from_event_log(path: Path, *, include_active: bool) -> list[dict[str, Any]]:
    """Return ordered model-switch operations; already-resident checks never emit these events."""
    if not path.is_file():
        return []
    loads: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            phase = event.get("phase")
            model = event.get("model")
            etype = event.get("type")
            if etype == events.MODEL_LOADING and isinstance(phase, str) and isinstance(model, str):
                loads.append(
                    {
                        "load_id": f"model-load-{len(loads) + 1}",
                        "phase": phase,
                        "label": event.get("label") or phase,
                        "model": model,
                        "started_at": event.get("ts"),
                        "finished_at": None,
                        "duration_s": None,
                        "status": "active",
                        "reason": None,
                    }
                )
            elif (
                etype == events.MODEL_LOAD_DONE
                and isinstance(phase, str)
                and isinstance(model, str)
            ):
                for load in reversed(loads):
                    if (
                        load["status"] != "active"
                        or load["phase"] != phase
                        or load["model"] != model
                    ):
                        continue
                    load["finished_at"] = event.get("ts")
                    load["duration_s"] = event.get("duration_s")
                    load["status"] = "completed" if event.get("success") is True else "failed"
                    load["reason"] = event.get("reason")
                    break
    return loads if include_active else [load for load in loads if load["status"] != "active"]


def _history_from_event_log(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Rebuild ordered executions from the append-only state history.

    Completed phases use their terminal event. A phase that was running when the process died is
    retained with a null verdict, so a live/interrupted breakdown still shows the latest state.
    """
    if not path.is_file():
        return []

    started: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    result: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except ValueError:
                warnings.append(f"{path.name}:{line_number}: malformed JSON line ignored")
                continue
            if not isinstance(event, dict):
                continue
            phase = event.get("phase")
            if not isinstance(phase, str):
                continue
            etype = event.get("type")
            if etype == events.PHASE_STARTED:
                started[phase].append(event)
            elif etype == events.SELF_CHECK_STARTED and started[phase]:
                started[phase][-1]["self_check_status"] = "active"
                started[phase][-1]["self_check_started_at"] = event.get("ts")
            elif etype == events.SELF_CHECK_DONE and started[phase]:
                verdict = event.get("verdict")
                started[phase][-1]["self_check_status"] = (
                    "passed" if verdict in ("DONE", "PASS") else "failed"
                )
                started[phase][-1]["self_check_duration_s"] = event.get("duration_s")
            elif etype == events.SELF_FIX_STARTED and started[phase]:
                started[phase][-1]["self_fix_status"] = "active"
                started[phase][-1]["self_fix_started_at"] = event.get("ts")
            elif etype == events.SELF_FIX_DONE and started[phase]:
                started[phase][-1]["self_fix_status"] = (
                    "completed" if event.get("repaired") is True else "failed"
                )
                duration = event.get("duration_s")
                if isinstance(duration, (int, float)):
                    previous = started[phase][-1].get("self_fix_duration_s")
                    started[phase][-1]["self_fix_duration_s"] = float(previous or 0) + duration
            elif etype in (events.PHASE_DONE, events.GATE_VERDICT):
                initial = started[phase].popleft() if started[phase] else {}
                result.append(
                    {
                        "phase": phase,
                        "label": event.get("label") or initial.get("label") or phase,
                        "attempt": initial.get("attempt"),
                        "ts": event.get("ts"),
                        "started_at": initial.get("ts"),
                        "finished_at": event.get("ts"),
                        "phase_type": initial.get("phase_type"),
                        "model": event.get("model") or initial.get("model"),
                        "duration_s": event.get("duration_s"),
                        "tools": event.get("tools"),
                        "verdict": event.get("verdict"),
                        "reason": event.get("reason"),
                        "self_check_status": initial.get("self_check_status", "not_run"),
                        "self_check_duration_s": initial.get("self_check_duration_s"),
                        "self_fix_status": initial.get("self_fix_status", "not_run"),
                        "self_fix_duration_s": initial.get("self_fix_duration_s"),
                    }
                )

    # Normally only one phase can be active, but preserving all unmatched starts is safer than
    # discarding evidence after a malformed or hand-edited log.
    active = [event for queue in started.values() for event in queue]
    active.sort(key=lambda event: float(event.get("ts", 0.0)))
    for event in active:
        result.append(
            {
                "phase": event["phase"],
                "label": event.get("label") or event["phase"],
                "attempt": event.get("attempt"),
                "ts": event.get("ts"),
                "started_at": event.get("ts"),
                "finished_at": None,
                "phase_type": event.get("phase_type"),
                "model": event.get("model"),
                "duration_s": None,
                "tools": None,
                "verdict": None,
                "reason": None,
                "self_check_status": event.get("self_check_status", "not_run"),
                "self_check_duration_s": event.get("self_check_duration_s"),
                "self_fix_status": event.get("self_fix_status", "not_run"),
                "self_fix_duration_s": event.get("self_fix_duration_s"),
            }
        )
    return result


def _run_options_from_event_log(path: Path) -> dict[str, bool]:
    """Read durable per-run options without coupling telemetry to live API state."""
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == events.RUN_STARTED:
                return {"clear_prefix_cache": event.get("clear_prefix_cache") is True}
    return {}


def _from_history(
    history: list[dict[str, Any]], sessions: list[dict[str, Any]], warnings: list[str]
) -> list[dict[str, Any]]:
    """History is authoritative for order and mechanical calls; transcripts supply usage."""
    by_phase: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for session in sessions:
        by_phase[str(session["phase"])].append(session)

    call_counts: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    for sequence, entry in enumerate(history, 1):
        phase = str(entry["phase"])
        call_counts[phase] += 1
        model = entry.get("model")
        expected_sessions = len(model.split("+")) if isinstance(model, str) and model else 1
        phase_sessions = by_phase[phase]
        started_at = entry.get("started_at")
        finished_at = entry.get("finished_at")
        if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
            attached: list[dict[str, Any]] = []
            while phase_sessions:
                session_started = phase_sessions[0].get("started_at")
                if not isinstance(session_started, (int, float)) or isinstance(
                    session_started, bool
                ):
                    # Pi creates the transcript before its first timestamped event is flushed.
                    # During that brief live-startup window, the unfinished phase and sequenced
                    # filename still identify the session unambiguously. Attach the expected root
                    # session so the dashboard does not report a transient omission.
                    if finished_at is None and len(attached) < expected_sessions:
                        attached.append(phase_sessions.popleft())
                    break
                if isinstance(finished_at, (int, float)) and session_started > finished_at:
                    break
                session = phase_sessions.popleft()
                session_finished = session.get("finished_at")
                if not isinstance(session_finished, (int, float)) or session_finished >= started_at:
                    attached.append(session)
        else:
            attached = [
                phase_sessions.popleft() for _ in range(min(expected_sessions, len(phase_sessions)))
            ]
        result.append(_phase_entry(sequence, call_counts[phase], entry, attached))

    leftovers = [session for queue in by_phase.values() for session in queue]
    if leftovers:
        warnings.append(
            f"{len(leftovers)} agent session(s) could not be matched to phase history; "
            "their statistics are omitted"
        )
    return result


def _from_sessions(sessions: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    """Return useful legacy statistics without inventing execution order or retry counts."""
    warnings.append(
        "phase history is unavailable; surviving sessions are unordered observations, not "
        "phase executions, and overwritten retries and mechanical phases cannot be reconstructed"
    )
    result: list[dict[str, Any]] = []
    for session in sessions:
        usage = session["usage"]
        tools = session["tool_calls_by_name"]
        result.append(
            {
                "phase": session["phase"],
                "model": session.get("model"),
                "duration_s": session.get("duration_s"),
                "context_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "reasoning_tokens": usage["reasoning_tokens"],
                "cache_read_tokens": usage["cache_read_tokens"],
                "cache_write_tokens": usage["cache_write_tokens"],
                "total_tokens": usage["total_tokens"],
                "context_window_tokens": usage["context_window_tokens"],
                "cost": usage["cost"],
                "tool_calls_total": sum(tools.values()),
                "tool_calls_by_name": dict(sorted(tools.items())),
            }
        )
    return result


def _phase_entry(
    sequence: int,
    call_number: int,
    history: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    usage = _empty_usage()
    session_tools: Counter[str] = Counter()
    for session in sessions:
        _add_usage(usage, session["usage"])
        session_tools.update(session["tool_calls_by_name"])
    # A Pi continuation is written to a new Quill transcript but retains the same session ID.
    # Its input snapshot already contains the earlier conversation, so adding both windows
    # fabricates context that never existed. Keep processed usage cumulative, but use only the
    # latest occupied window from each independent logical session.
    latest_windows: dict[str, tuple[float, int]] = {}
    for index, session in enumerate(sessions):
        logical_id = str(session.get("session_id") or session["transcript"])
        ordering = session.get("finished_at")
        order = float(ordering) if isinstance(ordering, (int, float)) else float(index)
        candidate = (order, int(session["usage"].get("context_window_tokens", 0)))
        if logical_id not in latest_windows or candidate[0] >= latest_windows[logical_id][0]:
            latest_windows[logical_id] = candidate

    history_tools = history.get("tools")
    tools = (
        Counter({str(k): int(v) for k, v in history_tools.items()})
        if isinstance(history_tools, dict)
        else session_tools
    )
    return {
        "sequence": sequence,
        "phase": history["phase"],
        "label": history.get("label") or history["phase"],
        "call_number": call_number,
        "is_retry": call_number > 1,
        "phase_type": history.get("phase_type"),
        "model": history.get("model") or _one_value(sessions, "model"),
        "verdict": history.get("verdict"),
        "rejection_reason": history.get("reason"),
        "self_check_status": history.get("self_check_status", "not_run"),
        "self_check_duration_s": history.get("self_check_duration_s"),
        "self_fix_status": history.get("self_fix_status", "not_run"),
        "self_fix_duration_s": history.get("self_fix_duration_s"),
        "started_at": history.get("started_at"),
        "finished_at": history.get("finished_at"),
        "duration_s": history.get("duration_s"),
        "context_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "total_tokens": usage["total_tokens"],
        "context_window_tokens": sum(value for _, value in latest_windows.values()),
        "cost": usage["cost"],
        "tool_calls_total": sum(tools.values()),
        "tool_calls_by_name": dict(sorted(tools.items())),
        "transcripts": [str(session["transcript"]) for session in sessions],
    }


def _parse_session(path: Path, warnings: list[str]) -> dict[str, Any]:
    usage = _empty_usage()
    settled_output = 0
    settled_reasoning = 0
    settled_cost = 0.0
    child_usage: dict[str, Any] = {}
    tools: Counter[str] = Counter()
    first_ts: float | None = None
    last_ts: float | None = None
    model: str | None = None
    provider: str | None = None
    session_id: str | None = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except ValueError:
                warnings.append(f"{path.name}:{line_number}: malformed JSON line ignored")
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "session" and isinstance(event.get("id"), str):
                session_id = event["id"]
            nested = pi_child_usage(event)
            if nested is not None:
                key, child_usage_snapshot = nested
                child_usage[key] = child_usage_snapshot
            timestamp = _timestamp(event)
            if timestamp is not None:
                first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
                last_ts = timestamp if last_ts is None else max(last_ts, timestamp)
            if event.get("type") == "tool_execution_start":
                tools[str(event.get("toolName") or "unknown")] += 1
            if event.get("type") == "message_end":
                message = event.get("message")
                if isinstance(message, dict):
                    model = message.get("model") if isinstance(message.get("model"), str) else model
                    provider = (
                        message.get("provider")
                        if isinstance(message.get("provider"), str)
                        else provider
                    )
                    raw_usage = message.get("usage")
                    if isinstance(raw_usage, dict):
                        normalized = _normalize_usage(raw_usage)
                        if _has_usage(normalized):
                            window_tokens = int(normalized["total_tokens"])
                            prior_output = settled_output
                            settled_output += int(normalized["output_tokens"])
                            settled_reasoning += int(normalized["reasoning_tokens"])
                            settled_cost += float(normalized["cost"])
                            normalized["output_tokens"] = settled_output
                            normalized["reasoning_tokens"] = settled_reasoning
                            normalized["cost"] = settled_cost
                            normalized["total_tokens"] += prior_output
                            normalized["context_window_tokens"] = window_tokens
                            usage = normalized

    _add_child_usage(usage, child_usage)

    match = _STREAM_RE.match(path.name)
    assert match is not None
    phase = match.group("body").split("-", 1)[0]
    return {
        "phase": phase,
        "transcript": path.name,
        "model": model,
        "provider": provider,
        "session_id": session_id,
        "started_at": first_ts,
        "finished_at": last_ts,
        "duration_s": _duration(first_ts, last_ts),
        "usage": usage,
        "tool_calls_by_name": dict(tools),
    }


def _one_value(items: list[dict[str, Any]], key: str) -> Any:
    values = {item.get(key) for item in items if item.get(key) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _timestamp(event: dict[str, Any]) -> float | None:
    value = event.get("timestamp")
    message = event.get("message")
    if value is None and isinstance(message, dict):
        value = message.get("timestamp")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) / 1000 if value > 10_000_000_000 else float(value)


def _empty_usage() -> dict[str, float | int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "context_window_tokens": 0,
        "cost": 0.0,
    }


def latest_usage(stream_path: Path) -> dict[str, float | int]:
    """The most recent cumulative token usage in a live phase transcript (or empty usage).

    The agent CLIs attach a running usage snapshot to each settled message, so the last one seen is
    the phase's current total. Called on live progress ticks to price a phase *while it runs*; it
    re-reads the flushed file each time (correctness over micro-efficiency — it is only the live
    display path, and the file is append-only so a concurrent read sees a consistent prefix).
    """
    usage = _empty_usage()
    settled_output = 0
    settled_reasoning = 0
    settled_cost = 0.0
    child_usage: dict[str, Any] = {}
    if not stream_path.is_file():
        return usage
    try:
        with stream_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                nested = pi_child_usage(event)
                if nested is not None:
                    key, child_usage_snapshot = nested
                    child_usage[key] = child_usage_snapshot
                if event.get("type") != "message_end":
                    continue
                message = event.get("message")
                raw = message.get("usage") if isinstance(message, dict) else None
                if isinstance(raw, dict):
                    normalized = _normalize_usage(raw)
                    if _has_usage(normalized):
                        window_tokens = int(normalized["total_tokens"])
                        prior_output = settled_output
                        settled_output += int(normalized["output_tokens"])
                        settled_reasoning += int(normalized["reasoning_tokens"])
                        settled_cost += float(normalized["cost"])
                        normalized["output_tokens"] = settled_output
                        normalized["reasoning_tokens"] = settled_reasoning
                        normalized["cost"] = settled_cost
                        normalized["total_tokens"] += prior_output
                        normalized["context_window_tokens"] = window_tokens
                        usage = normalized
    except OSError:
        pass
    _add_child_usage(usage, child_usage)
    return usage


def _add_child_usage(usage: dict[str, float | int], children: dict[str, Any]) -> None:
    """Add each subagent run's latest snapshot exactly once to its parent usage."""
    if not children:
        return
    usage["input_tokens"] += sum(child.input_tokens for child in children.values())
    usage["output_tokens"] += sum(child.output_tokens for child in children.values())
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["context_window_tokens"] += sum(
        child.context_window_tokens for child in children.values()
    )


def cumulative_live_usage(run_dir: Path) -> dict[str, float | int]:
    """Run-total live usage: the summed latest snapshot across **every** phase transcript.

    Phase-agnostic on purpose — completed phases contribute their final snapshot, the active phase
    its running one — so the live counters climb across the whole run instead of resetting each
    phase. ``total_tokens`` is normalized to ``input + output`` (the caller pays for both).
    """
    total = _empty_usage()
    latest_windows: dict[str, tuple[float, int]] = {}
    if run_dir.is_dir():
        for index, path in enumerate(sorted(run_dir.glob("stream-*.jsonl"))):
            usage = latest_usage(path)
            _add_usage(total, usage)
            session_id, finished_at = _session_identity(path)
            key = session_id or path.name
            order = finished_at if finished_at is not None else float(index)
            candidate = (order, int(usage["context_window_tokens"]))
            if key not in latest_windows or order >= latest_windows[key][0]:
                latest_windows[key] = candidate
    total["total_tokens"] = total["input_tokens"] + total["output_tokens"]
    total["context_window_tokens"] = sum(value for _, value in latest_windows.values())
    return total


def _session_identity(path: Path) -> tuple[str | None, float | None]:
    """Return a transcript's Pi session ID and latest timestamp without retaining payloads."""
    session_id: str | None = None
    latest: float | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "session" and isinstance(event.get("id"), str):
                    session_id = event["id"]
                timestamp = _timestamp(event)
                if timestamp is not None:
                    latest = timestamp if latest is None else max(latest, timestamp)
    except OSError:
        pass
    return session_id, latest


def _normalize_usage(raw: dict[str, Any]) -> dict[str, float | int]:
    cost = raw.get("cost")
    return {
        "input_tokens": _number(raw.get("input")),
        "output_tokens": _number(raw.get("output")),
        "reasoning_tokens": _number(raw.get("reasoning")),
        "cache_read_tokens": _number(raw.get("cacheRead")),
        "cache_write_tokens": _number(raw.get("cacheWrite")),
        "total_tokens": _number(raw.get("totalTokens")),
        "context_window_tokens": _number(raw.get("totalTokens")),
        "cost": _float_number(cost.get("total")) if isinstance(cost, dict) else 0.0,
    }


def _add_usage(total: dict[str, float | int], item: dict[str, float | int]) -> None:
    for key, value in item.items():
        total[key] += value


def _has_usage(usage: dict[str, float | int]) -> bool:
    """Return whether a runner event contains an actual usage snapshot."""
    return any(value != 0 for value in usage.values())


def _sum_final_usage(entries: list[dict[str, Any]]) -> dict[str, float | int]:
    """Sum one final snapshot per independent phase execution."""
    total = _empty_usage()
    field_map = {
        "input_tokens": "context_tokens",
        "output_tokens": "output_tokens",
        "reasoning_tokens": "reasoning_tokens",
        "cache_read_tokens": "cache_read_tokens",
        "cache_write_tokens": "cache_write_tokens",
        "total_tokens": "total_tokens",
        "context_window_tokens": "context_window_tokens",
        "cost": "cost",
    }
    for entry in entries:
        _add_usage(
            total,
            {usage_key: entry.get(entry_key, 0) for usage_key, entry_key in field_map.items()},
        )
    return {"context_tokens": total.pop("input_tokens"), **total}


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _float_number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _duration(start: Any, end: Any) -> float | None:
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    return round(max(0.0, float(end) - float(start)), 3)
