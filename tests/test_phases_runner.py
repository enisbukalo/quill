"""Phase runner + gate/retry tests (WI-4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quill.phases import (
    Outcome,
    PhaseResult,
    SpawnError,
    SpawnTimeout,
    run_gate,
    run_phase,
)


class FakeLoader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.loaded: list[str] = []

    def load(self, preset: str, timeout: float = 180) -> None:
        if self.fail:
            raise RuntimeError("CUDA OOM")
        self.loaded.append(preset)

    def unload_all(self) -> None: ...


def _done_stream() -> str:
    return json.dumps({"type": "text", "text": "DONE: ok | result: .plans/r.md"})


# -- run_phase --------------------------------------------------------------------


def test_run_phase_happy_path(tmp_path: Path) -> None:
    loader = FakeLoader()
    seen: list[Path] = []
    activity: list[str] = []

    def spawn(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        assert activity == ["model_loaded"]
        seen.append(stream_path)
        return _done_stream()

    r = run_phase(
        loader=loader,
        spawn=spawn,
        preset="plan-27b",
        agent="agent-plan",
        prompt="plan it",
        timeout=60,
        stream_path=tmp_path / "s.jsonl",
        on_model_loaded=lambda: activity.append("model_loaded"),
    )
    assert r.outcome is Outcome.DONE
    assert loader.loaded == ["plan-27b"]
    assert seen == [tmp_path / "s.jsonl"]  # run_phase forwards the stream path to spawn
    assert activity == ["model_loaded"]


def test_run_phase_load_failure_is_crash(tmp_path: Path) -> None:
    loader = FakeLoader(fail=True)
    activity: list[str] = []

    def spawn(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:  # pragma: no cover
        raise AssertionError("spawn should not run when load fails")

    r = run_phase(
        loader=loader,
        spawn=spawn,
        preset="x",
        agent="a",
        prompt="p",
        timeout=60,
        stream_path=tmp_path / "s.jsonl",
        on_model_loaded=lambda: activity.append("model_loaded"),
    )
    assert r.outcome is Outcome.CRASH
    assert "model load failed" in r.message
    assert activity == []


def test_run_phase_spawn_timeout_is_crash(tmp_path: Path) -> None:
    loader = FakeLoader()

    def spawn(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        raise SpawnTimeout("hung")

    r = run_phase(
        loader=loader,
        spawn=spawn,
        preset="x",
        agent="a",
        prompt="p",
        timeout=1,
        stream_path=tmp_path / "s.jsonl",
    )
    assert r.outcome is Outcome.CRASH


def test_run_phase_spawn_error_is_crash(tmp_path: Path) -> None:
    loader = FakeLoader()

    def spawn(
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: object = None,
        on_usage: object = None,
        abort_reason: object = None,
    ) -> str:
        raise SpawnError("exit 1")

    r = run_phase(
        loader=loader,
        spawn=spawn,
        preset="x",
        agent="a",
        prompt="p",
        timeout=1,
        stream_path=tmp_path / "s.jsonl",
    )
    assert r.outcome is Outcome.CRASH


# -- run_gate ---------------------------------------------------------------------

_BLOCK = PhaseResult(Outcome.BLOCK, "missing tests")
_PASS = PhaseResult(Outcome.PASS, "ok")


def _never(_: int) -> PhaseResult:  # pragma: no cover - asserted not called
    raise AssertionError("should not be called")


def test_gate_initial_pass_no_retries() -> None:
    res = run_gate(initial=_PASS, revise=_never, verify=_never, max_retries=1)
    assert res.passed
    assert res.attempts == 0


def test_gate_zero_retries_block_halts() -> None:
    res = run_gate(initial=_BLOCK, revise=_never, verify=_never, max_retries=0)
    assert not res.passed
    assert res.attempts == 0


def test_gate_revise_then_verify_passes() -> None:
    calls: list[str] = []

    def revise(attempt: int) -> PhaseResult:
        calls.append(f"revise{attempt}")
        return PhaseResult(Outcome.DONE, "revised")

    def verify(attempt: int) -> PhaseResult:
        calls.append(f"verify{attempt}")
        return _PASS

    res = run_gate(initial=_BLOCK, revise=revise, verify=verify, max_retries=2)
    assert res.passed
    assert res.attempts == 1
    assert calls == ["revise1", "verify1"]


def test_gate_exhausts_budget_then_halts() -> None:
    def revise(attempt: int) -> PhaseResult:
        return PhaseResult(Outcome.DONE, "revised")

    def verify(attempt: int) -> PhaseResult:
        return _BLOCK  # never passes

    res = run_gate(initial=_BLOCK, revise=revise, verify=verify, max_retries=2)
    assert not res.passed
    assert res.attempts == 2
    assert res.final.is_block


def test_gate_producer_crash_during_revise_surfaces() -> None:
    def revise(attempt: int) -> PhaseResult:
        return PhaseResult(Outcome.CRASH, "boom")

    res = run_gate(initial=_BLOCK, revise=revise, verify=_never, max_retries=2)
    assert not res.passed
    assert res.final.outcome is Outcome.CRASH


@pytest.mark.parametrize(
    "outcome",
    [Outcome.FAILED, Outcome.BLOCK, Outcome.CRASH, Outcome.GARBAGE, Outcome.NEEDS_DECISION],
)
def test_gate_never_verifies_after_unsuccessful_revise(outcome: Outcome) -> None:
    verify_attempts: list[int] = []

    def verify(attempt: int) -> PhaseResult:
        verify_attempts.append(attempt)
        return PhaseResult(Outcome.PASS, "must not run")

    revised = PhaseResult(outcome, "repair did not complete")
    result = run_gate(
        initial=_BLOCK,
        revise=lambda _attempt: revised,
        verify=verify,
        max_retries=2,
    )

    assert result.passed is False
    assert result.final is revised
    assert result.attempts == 1
    assert verify_attempts == []


def test_gate_verifier_non_verdict_stops_without_another_revision() -> None:
    revisions: list[int] = []

    def revise(attempt: int) -> PhaseResult:
        revisions.append(attempt)
        return PhaseResult(Outcome.DONE, "revised")

    garbage = PhaseResult(Outcome.GARBAGE, "missing receipt")
    res = run_gate(initial=_BLOCK, revise=revise, verify=lambda _attempt: garbage, max_retries=3)

    assert not res.passed
    assert res.final is garbage
    assert revisions == [1]


def test_gate_passthrough_non_verdict() -> None:
    """A CRASH/GARBAGE initial isn't a gate verdict — returned untouched."""
    crash = PhaseResult(Outcome.CRASH, "died")
    res = run_gate(initial=crash, revise=_never, verify=_never, max_retries=3)
    assert not res.passed
    assert res.final is crash
