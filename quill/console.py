"""Styled terminal output for the bare CLI (rich).

The pipeline emits plain event dicts (``quill.events``); this module renders them as colored,
symbol-prefixed lines for a human at a terminal. It is **presentation only** — the file log and
the API event stream consume the same raw events untouched.

rich auto-detects a non-TTY (piped/redirected) and drops the ANSI codes, so ``quill ... > log``
stays clean. Output goes to **stderr** so stdout is free for machine-readable use.
"""

from __future__ import annotations

import re
from typing import cast

from rich.console import Console
from rich.text import Text

from quill import events

# One console for the process, pinned to stderr. rich emits UTF-8 (so the glyphs below render on
# Windows code pages too) and auto-drops color when stderr is not a TTY (piped/redirected).
_console = Console(stderr=True, highlight=False, emoji=False)

# type -> (symbol, style). Color carries the meaning; the glyph is a quick visual anchor.
_EVENT_STYLE: dict[str, tuple[str, str]] = {
    events.RUN_STARTED: ("▶", "bold cyan"),
    events.RUN_PLAN: ("≡", "cyan"),
    events.PHASE_STARTED: ("◦", "blue"),
    events.MODEL_LOADING: ("◌", "cyan"),
    events.MODEL_LOAD_DONE: ("✔", "cyan"),
    events.PHASE_DONE: ("✔", "green"),
    events.GATE_VERDICT: ("✔", "green"),
    events.RETRY: ("↻", "yellow"),
    events.NEEDS_DECISION: ("?", "bold magenta"),
    events.RUN_HALTED: ("◼", "yellow"),
    events.RUN_DONE: ("✔", "bold green"),
    events.RUN_FAILED: ("✘", "bold red"),
}

# A phase verdict overrides the PHASE_DONE color so a BLOCK/FAIL reads red at a glance.
_VERDICT_STYLE: dict[str, tuple[str, str]] = {
    "PASS": ("✔", "green"),
    "DONE": ("✔", "green"),
    "BLOCK": ("✘", "red"),
    "FAILED": ("✘", "red"),
}


def render_event(event: dict[str, object]) -> Text:
    """Build the styled representation of an ``event`` (no I/O). Most events are one line; the run
    plan is a multi-line block."""
    etype = str(event.get("type", ""))

    # The run plan is a preformatted multi-line block; render it verbatim, dim after the header.
    if etype == events.RUN_PLAN:
        summary = str(event.get("summary", "")).splitlines() or ["run plan"]
        symbol, style = _EVENT_STYLE.get(events.RUN_PLAN, ("≡", "cyan"))
        text = Text()
        text.append(f"{symbol} ", style=style)
        text.append(summary[0], style=style)
        for line in summary[1:]:
            text.append("\n" + line, style="dim")
        return text

    symbol, style = _EVENT_STYLE.get(etype, ("•", "white"))

    # A verdict (on phase_done OR gate_verdict) recolors the line: BLOCK/FAIL red, PASS/DONE green.
    verdict = event.get("verdict")
    if etype in (events.PHASE_DONE, events.GATE_VERDICT) and isinstance(verdict, str):
        symbol, style = _VERDICT_STYLE.get(verdict.upper(), (symbol, style))
    if etype == events.MODEL_LOAD_DONE and event.get("success") is False:
        symbol, style = "✘", "red"

    text = Text()
    text.append(f"{symbol} ", style=style)

    label = event.get("label") or event.get("phase")
    headline = _headline(etype, str(label) if label else "")
    text.append(headline, style=style)

    # Phase type tag, e.g. " (reviewer)", on the start line — tells you what kind of phase ran.
    phase_type = event.get("phase_type")
    if etype == events.PHASE_STARTED and isinstance(phase_type, str) and phase_type:
        text.append(f" ({phase_type})", style="dim")

    detail = _detail(event)
    if detail:
        text.append(f"  {detail}", style="dim")

    # A BLOCK's reason is the point of the line — the judge saying what's wrong. The rest of the
    # detail is dim context, so a dim reason would read as more of the same; color it like the
    # verdict that carries it. Appended here rather than inside _detail() because that returns one
    # flat string styled wholesale, and this one span needs its own color.
    reason = event.get("reason")
    if (
        etype == events.GATE_VERDICT
        and str(event.get("verdict", "")).upper() == "BLOCK"
        and isinstance(reason, str)
        and reason.strip()
    ):
        text.append(f"  {_clip(reason.strip())}", style="red")
    return text


def print_event(event: dict[str, object]) -> None:
    """Render and print an event to the (stderr) console."""
    clear_progress()
    _console.print(render_event(event))


# -- live tool-call progress ------------------------------------------------------
#
# A phase spawn is a long silence: `phase_started` prints, then nothing until `phase_done` — impl
# routinely runs 100+ tool calls over tens of minutes, and a reader can't tell a working phase from
# a hung one (observed: a 41-minute impl phase where 31 minutes were dead spawns, diagnosable only
# by hand-parsing the JSONL transcript). So we tick a counter in place on its own line while the
# phase runs, then erase it — the permanent record is the `tools` field on `phase_done`.
#
# This is presentation only and deliberately NOT an event: 100+ counter events would flood the file
# log and the API stream, which consume the same event dicts (see module docstring).
_progress_active = False

# Tools shown on the live counter even at zero, in this order. A fixed set keeps the columns from
# reordering or appearing mid-phase as each tool is first used — the line stays the same shape from
# the first tick to the last, so a reader tracks a number changing rather than a layout moving.
# These are what pi and opencode actually emit (verified across every run in the vault); a tool
# outside the list still counts, appended after these in first-seen order rather than dropped.
_LIVE_TOOLS = ("read", "edit", "write", "bash")


def show_progress(tally: dict[str, int]) -> None:
    """Rewrite the live tool counter in place: ``⚒ 37  read ×20 · edit ×0 · write ×1 · bash ×16``.

    ``tally`` is the phase's counts so far. The known tools always render (``×0`` until used);
    anything else follows once seen.

    No-op when stderr isn't a TTY, so ``quill 2> log`` never collects carriage returns or a
    half-written counter line.
    """
    global _progress_active
    if not _console.is_terminal:
        return
    text = Text()
    text.append("⚒ ", style="magenta")
    text.append(str(sum(tally.values())), style="bold magenta")
    text.append("  ", style="dim")
    names = list(_LIVE_TOOLS) + [n for n in tally if n not in _LIVE_TOOLS]
    for i, name in enumerate(names):
        if i:
            text.append(" · ", style="dim")
        count = tally.get(name, 0)
        # Dim a tool that hasn't run yet so the eye lands on the ones actually working.
        text.append(f"{name} ×{count}", style="dim" if count == 0 else "magenta")
    # A leading \r + end="" parks the cursor on this one line so the next tick overwrites it;
    # the trailing erase clears any tail left by a previously longer line.
    _console.file.write("\r")
    _console.print(text, end="")
    _console.file.write("\x1b[K")  # erase to end of line
    _console.file.flush()
    _progress_active = True


def clear_progress() -> None:
    """Erase the live counter line, if one is showing, so the next event prints cleanly."""
    global _progress_active
    if not _progress_active:
        return
    _console.file.write("\r\x1b[K")
    _console.file.flush()
    _progress_active = False


def _headline(etype: str, label: str) -> str:
    """The bold part of the line — what just happened, phrased for a human."""
    if etype == events.RUN_STARTED:
        return "run started"  # ticket #/title trail in the dim detail
    if etype == events.RUN_DONE:
        return "run complete"
    if etype == events.RUN_FAILED:
        return "run failed"
    if etype == events.RUN_HALTED:
        return "run halted"
    if etype == events.NEEDS_DECISION:
        return "needs decision"
    if etype == events.RETRY:
        return f"retry {label}".rstrip()
    if etype == events.MODEL_LOADING:
        return f"loading model for {label}".rstrip()
    if etype == events.MODEL_LOAD_DONE:
        return f"model load for {label}".rstrip()
    return label or etype


def format_tools(tools: dict[str, int]) -> str:
    """A phase's tool tally as ``edit ×24 · read ×31 · bash ×12``, busiest tool first.

    Shared by the console and the file log so a phase reads the same in both.
    """
    ranked = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{name} ×{count}" for name, count in ranked)


def _detail(event: dict[str, object]) -> str:
    """Trailing dim context: ticket, model, duration, counters, verdicts, reasons, urls."""
    etype = str(event.get("type", ""))
    parts: list[str] = []

    # run_started: ticket number + title up front.
    if etype == events.RUN_STARTED:
        ticket = event.get("ticket")
        title = event.get("title")
        head = f"#{ticket}" if isinstance(ticket, int) else ""
        if isinstance(title, str) and title.strip():
            head = f"{head} {title.strip()}".strip()
        if head:
            parts.append(head)

    model = event.get("model")
    if isinstance(model, str) and model.strip():
        parts.append(model.strip())

    session_capacity = event.get("session_capacity")
    if etype == events.MODEL_LOADING and isinstance(session_capacity, int):
        parts.append(f"audit capacity {session_capacity}")

    attempt, max_attempts = event.get("attempt"), event.get("max_attempts")
    if isinstance(attempt, int) and isinstance(max_attempts, int) and max_attempts > 1:
        parts.append(f"attempt {attempt}/{max_attempts}")

    verdict = event.get("verdict")
    if etype in (events.PHASE_DONE, events.GATE_VERDICT) and isinstance(verdict, str) and verdict:
        parts.append(verdict)

    duration = event.get("duration_s")
    if isinstance(duration, (int, float)):
        parts.append(f"{duration:.2f}s")

    tools = event.get("tools")
    if isinstance(tools, dict) and tools:
        parts.append(format_tools(cast(dict[str, int], tools)))

    for key in ("reason", "question", "pr_url"):
        # A gate verdict's reason is appended by the caller in its own color, not folded into this
        # dim tail — skip it here so it isn't printed twice.
        if key == "reason" and etype == events.GATE_VERDICT:
            continue
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    return "  ".join(parts)


#: Longest reason rendered on a console line. A judge's BLOCK receipt is a full sentence or three
#: ("BLOCK: 3 unmet MAJOR findings — (1) ... (2) ...") and printing it whole wraps the terminal and
#: buries the phase lines around it. The file log keeps the untruncated text, and the findings file
#: has the full argument — this is the at-a-glance version.
_REASON_MAX = 90

#: The receipt verb a judge's message opens with. The line already prints the verdict, so echoing
#: "BLOCK:" back in the reason spends scarce width on a word that's two columns to the left.
_RECEIPT_PREFIX = re.compile(r"^(BLOCK|FAILED|PASS|DONE)\s*:\s*", re.IGNORECASE)


def _clip(text: str) -> str:
    """``text`` as one clipped line: receipt verb dropped, whitespace collapsed, ellipsis if long."""
    flat = " ".join(text.split())  # collapse newlines/runs — a reason must not break the line
    flat = _RECEIPT_PREFIX.sub("", flat)
    if len(flat) <= _REASON_MAX:
        return flat
    return flat[: _REASON_MAX - 1].rstrip() + "…"
