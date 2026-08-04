"""Receipt parser/classifier unit tests (WI-4)."""

from __future__ import annotations

import json

import pytest

from quill.phases import (
    Outcome,
    classify_output,
    classify_receipt,
    extract_receipt,
)


@pytest.mark.parametrize(
    ("receipt", "outcome"),
    [
        ("DONE: wrote plan to .plans/plan.md", Outcome.DONE),
        ("FAILED: could not read the ticket", Outcome.FAILED),
        ("PASS: plan covers all acceptance criteria", Outcome.PASS),
        ("BLOCK: plan missing error handling", Outcome.BLOCK),
    ],
)
def test_basic_verbs(receipt: str, outcome: Outcome) -> None:
    assert classify_receipt(receipt).outcome is outcome


def test_done_extracts_result_path() -> None:
    r = classify_receipt("DONE: implemented | result: .plans/worker-result.md")
    assert r.outcome is Outcome.DONE
    assert r.result_path == ".plans/worker-result.md"


def test_needs_decision_emdash() -> None:
    r = classify_receipt("FAILED: needs decision — which DB backend? | result: .plans/q.md")
    assert r.outcome is Outcome.NEEDS_DECISION
    assert r.needs_decision
    assert r.question == "which DB backend?"
    assert r.result_path == ".plans/q.md"


def test_needs_decision_hyphen_and_no_result() -> None:
    r = classify_receipt("FAILED: needs decision - rename the module?")
    assert r.outcome is Outcome.NEEDS_DECISION
    assert r.question == "rename the module?"
    assert r.result_path is None


def test_needs_decision_takes_precedence_over_failed() -> None:
    """A needs-decision line is a FAILED subtype but must not classify as plain FAILED."""
    r = classify_receipt("FAILED: needs decision — pick a name")
    assert r.outcome is Outcome.NEEDS_DECISION


def test_no_receipt_is_garbage() -> None:
    assert classify_receipt(None).outcome is Outcome.GARBAGE


def test_unparsable_receipt_is_garbage() -> None:
    r = classify_receipt("here is my answer, hope it helps!")
    assert r.outcome is Outcome.GARBAGE
    assert r.raw_receipt == "here is my answer, hope it helps!"


def test_crlf_and_whitespace_tolerated() -> None:
    assert classify_receipt("  PASS: looks good \r").outcome is Outcome.PASS


@pytest.mark.parametrize(
    "wrapped",
    [
        "(DONE: implemented | result: /tmp/impl.md)",
        "- PASS: requirements satisfied",
        "> BLOCK: missing lifecycle coverage",
        "`FAILED: could not write artifact`",
        "**DONE: implementation complete**",
    ],
)
def test_classify_normalizes_harmless_receipt_wrappers(wrapped: str) -> None:
    assert classify_receipt(wrapped).outcome is not Outcome.GARBAGE


@pytest.mark.parametrize(
    "ambiguous",
    [
        "The result is (DONE: implemented)",
        "(DONE: implemented) trailing prose",
        "- I think DONE: implemented",
        "`DONE: implemented",
    ],
)
def test_classify_does_not_salvage_embedded_or_unbalanced_receipts(ambiguous: str) -> None:
    assert classify_receipt(ambiguous).outcome is Outcome.GARBAGE


# -- extract_receipt over a JSON stream -------------------------------------------


def _stream(*objs: dict[str, object]) -> str:
    return "\n".join(json.dumps(o) for o in objs)


def test_extract_last_text_part() -> None:
    stdout = _stream(
        {"type": "text", "text": "thinking..."},
        {"type": "tool", "name": "write"},
        {"type": "text", "text": "DONE: finished | result: .plans/r.md"},
    )
    assert extract_receipt(stdout) == "DONE: finished | result: .plans/r.md"


def test_extract_nested_part_shape() -> None:
    stdout = _stream({"part": {"type": "text", "text": "PASS: ok"}})
    assert extract_receipt(stdout) == "PASS: ok"


def test_extract_normalizes_parenthesized_receipt() -> None:
    stdout = _stream({"type": "text", "text": "(DONE: complete | result: /tmp/impl.md)"})
    assert extract_receipt(stdout) == "DONE: complete | result: /tmp/impl.md"


def test_extract_ignores_non_json_noise() -> None:
    stdout = "loading model...\n" + _stream({"type": "text", "text": "DONE: ok"}) + "\n\r"
    assert extract_receipt(stdout) == "DONE: ok"


def test_extract_none_when_no_text() -> None:
    stdout = _stream({"type": "tool", "name": "edit"})
    assert extract_receipt(stdout) is None


def test_classify_output_end_to_end() -> None:
    stdout = _stream(
        {"type": "text", "text": "working"},
        {"type": "text", "text": "BLOCK: tests missing"},
    )
    assert classify_output(stdout).outcome is Outcome.BLOCK


def test_extract_salvages_verdict_before_trailing_chatter() -> None:
    """A chatty model emits its verdict, then keeps reasoning in a later text part. The receipt is
    the verdict line, not the trailing chatter — else a valid PASS/BLOCK is lost as GARBAGE."""
    stdout = _stream(
        {"type": "text", "text": "PASS: plan is complete and correct"},
        {"type": "text", "text": "Now let me verify the plan's claims about main.cpp:174."},
    )
    assert extract_receipt(stdout) == "PASS: plan is complete and correct"
    assert classify_output(stdout).outcome is Outcome.PASS


def test_extract_salvages_verdict_mid_part() -> None:
    """The verdict line sits among reasoning lines inside a single text part."""
    stdout = _stream(
        {
            "type": "text",
            "text": "Let me judge the plan.\nBLOCK: missing error handling in phase 2\nI should double-check.",
        }
    )
    assert extract_receipt(stdout) == "BLOCK: missing error handling in phase 2"
    assert classify_output(stdout).outcome is Outcome.BLOCK


def test_extract_needs_decision_salvaged_over_chatter() -> None:
    stdout = _stream(
        {"type": "text", "text": "FAILED: needs decision — which DB? | result: plan.md"},
        {"type": "text", "text": "Meanwhile I'll note the options above."},
    )
    assert classify_output(stdout).outcome is Outcome.NEEDS_DECISION


def test_extract_last_receipt_wins_when_multiple() -> None:
    """If the model restates a verdict, the last receipt-shaped line is the final answer."""
    stdout = _stream(
        {"type": "text", "text": "BLOCK: first pass had issues"},
        {"type": "text", "text": "PASS: on reflection the plan is fine"},
    )
    assert extract_receipt(stdout) == "PASS: on reflection the plan is fine"


def test_extract_falls_back_to_last_text_when_no_receipt() -> None:
    """No receipt-shaped line anywhere → last text part, so classify still reports GARBAGE."""
    stdout = _stream(
        {"type": "text", "text": "hmm"},
        {"type": "text", "text": "still thinking about it"},
    )
    assert extract_receipt(stdout) == "still thinking about it"
    assert classify_output(stdout).outcome is Outcome.GARBAGE
