"""Run-state persistence + resume guard tests (ticket #33)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill import events
from quill.config import PhaseDef, QuillfolioConfig
from quill.runstate_file import (
    ResumeError,
    RunStateFile,
    make_recorder,
    read_last_run_id,
    read_state,
    resume_target,
    state_path,
    write_state,
)


def _config(tmp_path: Path, phases: list[PhaseDef] | None = None) -> QuillfolioConfig:
    return QuillfolioConfig(
        directory=tmp_path,
        repo="me/proj",
        pr_base="main",
        runner="opencode",
        build_command="make",
        test_command="make test",
        log_dir="logs",
        phases=phases
        or [
            PhaseDef(id="plan", type="producer", persona="p.md", models=("m",)),
            PhaseDef(id="impl", type="producer", persona="p.md", models=("m",)),
        ],
    )


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = state_path(config, "run1")
    write_state(path, RunStateFile(ticket=42, run_id="run1", phase="impl", status="failed"))
    state = read_state(path)
    assert state is not None
    assert state.ticket == 42
    assert state.run_id == "run1"
    assert state.phase == "impl"
    assert state.status == "failed"


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_state(tmp_path / "nope.json") is None


def test_read_garbage_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("not json", encoding="utf-8")
    assert read_state(p) is None


def test_recorder_persists_each_transition(tmp_path: Path) -> None:
    config = _config(tmp_path)
    seen: list[dict[str, object]] = []
    on_event = make_recorder(config, ticket=42, run_id="run1", base_on_event=seen.append)

    on_event(events.run_started("run1", 42))
    on_event(events.phase_started("plan", "write plan"))
    assert [e["type"] for e in seen] == [events.RUN_STARTED, events.PHASE_STARTED]
    s1 = read_state(state_path(config, "run1"))
    assert s1 is not None and s1.phase == "plan" and s1.status == "running"
    # The pointer records this as the latest run.
    assert read_last_run_id(config) == "run1"

    on_event(events.needs_decision("which db?", phase="plan"))
    s2 = read_state(state_path(config, "run1"))
    assert s2 is not None and s2.status == "needs_decision" and s2.question == "which db?"

    on_event(events.run_halted(reason="needs decision", phase="plan"))
    s3 = read_state(state_path(config, "run1"))
    assert s3 is not None and s3.status == "halted"


def test_recorder_marks_done_and_stamps_hash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    on_event = make_recorder(config, ticket=1, run_id="run1", base_on_event=lambda _e: None)
    on_event(events.phase_started("impl", "implement"))
    on_event(events.run_done(pr_url="https://x/pull/1"))
    state = read_state(state_path(config, "run1"))
    assert state is not None and state.status == "done"
    assert state.phase_set_hash == config.phase_set_hash()


# -- resume guard -----------------------------------------------------------------


def _record_halt_at(config: QuillfolioConfig, ticket: int, run_id: str, phase: str) -> None:
    on_event = make_recorder(config, ticket=ticket, run_id=run_id, base_on_event=lambda _e: None)
    on_event(events.phase_started(phase, phase))
    on_event(events.run_halted(reason="stopped", phase=phase))


def test_resume_target_returns_run_and_phase(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _record_halt_at(config, ticket=7, run_id="run-A", phase="impl")
    run_id, start, clear_prefix_cache = resume_target(config, ticket=7)
    assert run_id == "run-A"
    assert start == "impl"
    assert clear_prefix_cache is False


def test_resume_preserves_prefix_cache_choice(tmp_path: Path) -> None:
    config = _config(tmp_path)
    on_event = make_recorder(
        config,
        ticket=7,
        run_id="run-A",
        base_on_event=lambda _e: None,
        clear_prefix_cache=True,
    )
    on_event(events.phase_started("impl", "impl"))
    on_event(events.run_halted(reason="stopped", phase="impl"))
    assert resume_target(config, ticket=7) == ("run-A", "impl", True)


def test_resume_no_saved_run_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(ResumeError, match="nothing to resume"):
        resume_target(config, ticket=7)


def test_resume_wrong_ticket_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _record_halt_at(config, ticket=7, run_id="run-A", phase="impl")
    with pytest.raises(ResumeError, match="for ticket 7, not 8"):
        resume_target(config, ticket=8)


def test_resume_done_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    on_event = make_recorder(config, ticket=7, run_id="run-A", base_on_event=lambda _e: None)
    on_event(events.phase_started("impl", "impl"))
    on_event(events.run_done())
    with pytest.raises(ResumeError, match="already completed"):
        resume_target(config, ticket=7)


def test_resume_config_changed_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _record_halt_at(config, ticket=7, run_id="run-A", phase="impl")
    # Now the config changes: a different phase set -> different hash.
    changed = _config(
        tmp_path,
        phases=[
            PhaseDef(id="plan", type="producer", persona="p.md", models=("m",)),
            PhaseDef(id="impl", type="producer", persona="p.md", models=("m",)),
            PhaseDef(id="extra", type="producer", persona="p.md", models=("m",)),
        ],
    )
    with pytest.raises(ResumeError, match="phase config changed"):
        resume_target(changed, ticket=7)


def test_resume_phase_not_in_config_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # Hand-write a state whose phase doesn't exist and whose hash matches (forced).
    write_state(
        state_path(config, "run-A"),
        RunStateFile(
            ticket=7,
            run_id="run-A",
            phase="ghost",
            status="halted",
            phase_set_hash=config.phase_set_hash(),
        ),
    )
    (config.runs_root / "last-run.json").write_text('{"run_id": "run-A"}', encoding="utf-8")
    with pytest.raises(ResumeError, match="not in the current config"):
        resume_target(config, ticket=7)
