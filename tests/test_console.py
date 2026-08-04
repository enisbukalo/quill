"""Tests for the styled CLI console (quill.console)."""

from __future__ import annotations

from quill import events
from quill.console import render_event


def _plain(event: dict[str, object]) -> str:
    """The rendered line as plain text (styles stripped) — what a non-TTY would show."""
    return render_event(event).plain


def test_phase_started_renders_label() -> None:
    line = _plain(events.phase_started("plan", "write plan"))
    assert "write plan" in line


def test_phase_done_shows_verdict() -> None:
    line = _plain(events.phase_done("review_plan", "review plan", verdict="PASS"))
    assert "review plan" in line
    assert "PASS" in line


def test_block_verdict_styled_red() -> None:
    # A BLOCK recolors the line red even though the event type is the same phase_done.
    text = render_event(events.phase_done("review_plan", "review plan", verdict="BLOCK"))
    assert any("red" in str(span.style) for span in text.spans)


def test_pass_verdict_styled_green() -> None:
    text = render_event(events.phase_done("plan", "plan", verdict="PASS"))
    assert any("green" in str(span.style) for span in text.spans)


def test_retry_shows_attempt_and_reason() -> None:
    line = _plain(events.retry("review_plan", attempt=2, max_attempts=3, reason="missing tests"))
    assert "retry" in line
    assert "attempt 2/3" in line
    assert "missing tests" in line


def test_run_done_shows_pr_url() -> None:
    line = _plain(events.run_done(pr_url="https://example.com/pr/9"))
    assert "run complete" in line
    assert "https://example.com/pr/9" in line


def test_run_failed_shows_reason() -> None:
    line = _plain(events.run_failed(reason="build failed", phase="build_test"))
    assert "run failed" in line
    assert "build failed" in line


def test_needs_decision_shows_question() -> None:
    line = _plain(events.needs_decision("which db?", phase="plan"))
    assert "needs decision" in line
    assert "which db?" in line


def test_single_attempt_omits_counter() -> None:
    # attempt 1/1 is noise — the counter only shows when there's more than one attempt.
    line = _plain(events.phase_started("plan", "write plan", attempt=1, max_attempts=1))
    assert "attempt" not in line


def test_unknown_event_type_falls_back() -> None:
    line = _plain({"type": "mystery", "phase": "x"})
    assert line  # renders something rather than crashing


def test_run_started_shows_ticket_and_title() -> None:
    line = _plain(events.run_started(run_id="r", ticket=126, title="Fix the thing"))
    assert "#126" in line
    assert "Fix the thing" in line


def test_phase_started_shows_type_and_model() -> None:
    line = _plain(events.phase_started("plan", "write plan", phase_type="producer", model="m-27b"))
    assert "(producer)" in line
    assert "m-27b" in line


def test_phase_done_shows_model_and_duration() -> None:
    line = _plain(
        events.phase_done("plan", "write plan", verdict="DONE", model="m-27b", duration_s=42.7)
    )
    assert "m-27b" in line
    assert "42.70s" in line  # two-decimal seconds


def test_gate_verdict_shows_model_duration_and_recolors() -> None:
    event = events.gate_verdict(
        "review_plan", "BLOCK", label="review plan", model="r-27b", duration_s=18.2
    )
    line = _plain(event)
    assert "BLOCK" in line
    assert "r-27b" in line
    assert "18.20s" in line
    # BLOCK recolors the gate_verdict line red.
    assert any("red" in str(span.style) for span in render_event(event).spans)


def test_fanout_model_label_joined() -> None:
    line = _plain(
        events.phase_started("review_impl", "review impl", phase_type="reviewer", model="a+b")
    )
    assert "a+b" in line
