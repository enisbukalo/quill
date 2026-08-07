"""Runner registry, pi receipt parsing, and preflight tests (WI-16)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from quill.phases import Outcome, classify_receipt
from quill.preflight import PreflightError
from quill.runners import (
    UnknownRunnerError,
    available_runners,
    get_runner,
)
from quill.runners.opencode import OpencodeRunner
from quill.runners.pi import PiRunner, extract_pi_receipt
from quill.runners.git_guard import agent_environment
from quill.runctx import PipelineDeps

# -- registry ---------------------------------------------------------------------


def test_builtin_runners_registered() -> None:
    assert "opencode" in available_runners()
    assert "pi" in available_runners()


def test_get_runner_resolves_by_name() -> None:
    assert isinstance(get_runner("opencode", directory="."), OpencodeRunner)
    assert isinstance(get_runner("pi", directory="."), PiRunner)


def test_get_runner_is_case_insensitive_and_trims() -> None:
    assert isinstance(get_runner("  PI ", directory="."), PiRunner)


def test_pi_skill_directive_uses_slash_skill_syntax() -> None:
    r = PiRunner(directory=".")
    line = r.skill_directive(["cpp-pro", "plan-mode"])
    assert "/skill:cpp-pro" in line
    assert "/skill:plan-mode" in line
    assert r.skill_directive([]) == ""  # no skills => nothing appended


def _write_pi_models(agent_dir: Path, providers: dict[str, list[dict[str, object]]]) -> None:
    agent_dir.mkdir(parents=True)
    payload = {
        "providers": {provider: {"models": models} for provider, models in providers.items()}
    }
    (agent_dir / "models.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_proc_entry(proc_root: Path, pid: int, *, child: bool = False) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir(parents=True)
    (process_dir / "comm").write_text("pi\n", encoding="utf-8")
    environment = b"PATH=/bin\0"
    if child:
        environment += b"PI_SUBAGENT_CHILD=1\0"
    (process_dir / "environ").write_bytes(environment)


def test_pi_capacity_uses_agent_override_and_deducts_live_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import quill.runners.pi as pi_mod

    agent_dir = tmp_path / "agent"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_pi_models(
        agent_dir,
        {"vllm": [{"id": "qwen", "subagentConcurrency": 2}]},
    )
    _write_proc_entry(proc_root, 100)
    _write_proc_entry(proc_root, 101, child=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(pi_mod, "_PROC_ROOT", proc_root)

    assert PiRunner(directory=".").available_session_capacity("qwen") == 2


def test_pi_capacity_supports_provider_qualified_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import quill.runners.pi as pi_mod

    agent_dir = tmp_path / "agent"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_pi_models(
        agent_dir,
        {
            "vllm": [{"id": "shared", "subagentConcurrency": 2}],
            "other": [{"id": "shared", "subagentConcurrency": 4}],
        },
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(pi_mod, "_PROC_ROOT", proc_root)

    runner = PiRunner(directory=".")
    assert runner.available_session_capacity("vllm/shared") == 3
    assert runner.available_session_capacity("other:shared") == 5
    assert runner.available_session_capacity("shared") == 1


@pytest.mark.parametrize(
    "model_entry",
    [
        {"id": "qwen"},
        {"id": "qwen", "subagentConcurrency": 0},
        {"id": "qwen", "subagentConcurrency": True},
        {"id": "qwen", "subagentConcurrency": "2"},
    ],
)
def test_pi_capacity_falls_back_for_invalid_registry_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model_entry: dict[str, object]
) -> None:
    import quill.runners.pi as pi_mod

    agent_dir = tmp_path / "agent"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_pi_models(agent_dir, {"vllm": [model_entry]})
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(pi_mod, "_PROC_ROOT", proc_root)

    assert PiRunner(directory=".").available_session_capacity("qwen") == 1


def test_pi_capacity_falls_back_when_registry_or_proc_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import quill.runners.pi as pi_mod

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "missing-agent"))
    monkeypatch.setattr(pi_mod, "_PROC_ROOT", tmp_path / "missing-proc")
    assert PiRunner(directory=".").available_session_capacity("qwen") == 1


def test_opencode_skill_directive_uses_slash_name_syntax() -> None:
    r = OpencodeRunner(directory=".")
    line = r.skill_directive(["cpp-pro", "review-mode"])
    assert "/cpp-pro" in line
    assert "/review-mode" in line
    assert "/skill:" not in line  # opencode syntax, not pi's
    assert r.skill_directive([]) == ""


def test_runner_capacity_defaults_to_one() -> None:
    assert OpencodeRunner(directory=".").available_session_capacity("any-model") == 1


def test_agent_git_guard_denies_mutations_outside_delivery_phases(tmp_path: Path) -> None:
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    git.chmod(0o700)
    inherited = {"PATH": str(tmp_path)}

    with agent_environment("update_impl", inherited) as environment:
        wrapper = Path(environment["PATH"].split(":", 1)[0]) / "git"
        result = subprocess.run(
            [str(wrapper), "commit", "-m", "forbidden"],
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 77
    assert "only commit/commit_update may mutate Git" in result.stderr


def test_agent_git_guard_leaves_delivery_phase_environment_unchanged(tmp_path: Path) -> None:
    inherited = {"PATH": str(tmp_path), "EXAMPLE": "yes"}

    with agent_environment("commit_update", inherited) as environment:
        assert environment == inherited


class _Loader:
    def load(self, preset: str, timeout: float = 180) -> None:
        _ = preset, timeout

    def unload_all(self) -> None:
        return None


def test_pipeline_deps_clamps_invalid_runner_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = OpencodeRunner(directory=".")
    monkeypatch.setattr(OpencodeRunner, "available_session_capacity", lambda _self, _model: 0)
    deps = PipelineDeps.with_runner(runner, loader=_Loader())
    assert deps.session_capacity("model") == 1


def test_pipeline_deps_contains_runner_capacity_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = OpencodeRunner(directory=".")

    def fail(_self: object, _model: str) -> int:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(OpencodeRunner, "available_session_capacity", fail)
    deps = PipelineDeps.with_runner(runner, loader=_Loader())
    assert deps.session_capacity("model") == 1


def test_pipeline_deps_cancels_runner_and_mechanical_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cancelled: list[bool] = []
    monkeypatch.setattr(OpencodeRunner, "cancel", lambda _self: runner_cancelled.append(True))
    runner = OpencodeRunner(directory=".")

    class BuildRunner:
        def __init__(self) -> None:
            self.cancelled = False

        def __call__(self, _config: object, _selection: str) -> tuple[bool, str]:
            return True, "ok"

        def cancel(self) -> None:
            self.cancelled = True

    build_runner = BuildRunner()
    deps = PipelineDeps.with_runner(runner, loader=_Loader(), build_test=build_runner)  # type: ignore[arg-type]

    assert deps.cancel_active is not None
    deps.cancel_active()

    assert runner_cancelled == [True]
    assert build_runner.cancelled


def test_get_runner_unknown_lists_available() -> None:
    with pytest.raises(UnknownRunnerError) as exc:
        get_runner("aider", directory=".")
    msg = str(exc.value)
    assert "aider" in msg
    assert "opencode" in msg and "pi" in msg


# -- pi receipt extraction --------------------------------------------------------


def _stream(*objs: dict[str, object]) -> str:
    return "\n".join(json.dumps(o) for o in objs)


def _msg_end(*parts: dict[str, object]) -> dict[str, object]:
    return {"type": "message_end", "message": {"content": list(parts)}}


def test_pi_extracts_final_text_from_message_end() -> None:
    stdout = _stream(
        {"type": "message_start"},
        _msg_end({"type": "text", "text": "DONE: shipped | result: .plans/r.md"}),
    )
    assert extract_pi_receipt(stdout) == "DONE: shipped | result: .plans/r.md"


def test_pi_uses_last_message_end_and_last_text_part() -> None:
    stdout = _stream(
        _msg_end({"type": "text", "text": "PASS: first"}),
        _msg_end(
            {"type": "tool_use", "name": "write"},
            {"type": "text", "text": "intermediate"},
            {"type": "text", "text": "BLOCK: tests missing"},
        ),
    )
    assert extract_pi_receipt(stdout) == "BLOCK: tests missing"


def test_pi_receipt_line_after_chatty_preamble() -> None:
    """Regression: a text part with prose BEFORE the receipt line must still classify on the receipt.

    Small models emit a preamble ("Done. File confirmed on disk...") then the real DONE: line in the
    SAME text part. classify_receipt anchors at string start, so returning the whole part
    misclassified a real DONE as GARBAGE (the commit phase failed with its own DONE as the reason).
    The extractor must return the receipt LINE, not the whole part.
    """
    part = "Done. File confirmed on disk (46 lines, 2156 bytes).\n\nDONE: wrote commit + PR text | result: /x/commit.md"
    stdout = _stream(_msg_end({"type": "text", "text": part}))
    receipt = extract_pi_receipt(stdout)
    assert receipt == "DONE: wrote commit + PR text | result: /x/commit.md"
    assert classify_receipt(receipt).outcome is Outcome.DONE


def test_pi_normalizes_parenthesized_receipt_from_finalizer_regression() -> None:
    """Ticket #17 failed after Pi repeated a valid DONE receipt inside parentheses."""
    text = "(DONE: Ticket #17 finalized | result: /home/user/.quill/runs/run17/impl.md)"
    stdout = _stream(_msg_end({"type": "text", "text": text}))

    receipt = extract_pi_receipt(stdout)

    assert receipt == ("DONE: Ticket #17 finalized | result: /home/user/.quill/runs/run17/impl.md")
    result = classify_receipt(receipt)
    assert result.outcome is Outcome.DONE
    assert result.result_path == "/home/user/.quill/runs/run17/impl.md"


def test_pi_no_receipt_line_falls_back_to_whole_part() -> None:
    """No receipt-shaped line anywhere => return the whole part so it still reads as GARBAGE."""
    stdout = _stream(_msg_end({"type": "text", "text": "just chatting, no verdict here"}))
    assert extract_pi_receipt(stdout) == "just chatting, no verdict here"


def test_pi_ignores_noise_and_non_message_events() -> None:
    stdout = (
        "loading pi...\n"
        + _stream(
            {"type": "turn_start"},
            {"type": "tool_execution_end", "result": {}},
            _msg_end({"type": "text", "text": "DONE: ok"}),
        )
        + "\n\r"
    )
    assert extract_pi_receipt(stdout) == "DONE: ok"


def test_pi_no_message_end_is_none() -> None:
    stdout = _stream({"type": "turn_start"}, {"type": "tool_execution_start"})
    assert extract_pi_receipt(stdout) is None


def test_pi_ignores_user_prompt_when_assistant_connection_fails() -> None:
    stdout = _stream(
        {
            "type": "message_end",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Continue and emit DONE"}],
            },
        },
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [], "stopReason": "error"},
        },
    )
    assert extract_pi_receipt(stdout) is None


def test_pi_message_end_without_text_part_is_none() -> None:
    stdout = _stream(_msg_end({"type": "tool_use", "name": "edit"}))
    assert extract_pi_receipt(stdout) is None


def test_pi_receipt_classifies_end_to_end() -> None:
    stdout = _stream(_msg_end({"type": "text", "text": "FAILED: needs decision — which db?"}))
    result = classify_receipt(extract_pi_receipt(stdout))
    assert result.outcome is Outcome.NEEDS_DECISION
    assert result.question == "which db?"


# -- spawn argv -------------------------------------------------------------------


def test_pi_spawn_builds_headless_json_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import quill.runners.pi as pi_mod

    captured: dict[str, object] = {}

    def fake_stream(
        cmd,
        *,
        cwd,
        stream_path,
        agent,
        timeout,
        input_text=None,
        env=None,
        on_tool=None,
        on_usage=None,
        should_stop=None,
        abort_reason=None,
    ):  # type: ignore[no-untyped-def]
        captured.update(
            cmd=cmd,
            cwd=cwd,
            stream_path=stream_path,
            agent=agent,
            timeout=timeout,
            input_text=input_text,
            should_stop=should_stop,
        )
        return "STREAMED STDOUT"

    monkeypatch.setattr(pi_mod, "run_streaming", fake_stream)
    monkeypatch.setattr(pi_mod.shutil, "which", lambda _name: "/usr/bin/pi")
    stream = tmp_path / "stream-plan.jsonl"
    out = PiRunner(directory=str(tmp_path)).spawn(
        "agent-plan", "plan-27b", "do it", timeout=90, stream_path=stream
    )
    assert out == "STREAMED STDOUT"
    cmd = cast("list[str]", captured["cmd"])
    assert cmd[0] == "/usr/bin/pi"  # resolved executable, not a bare "pi"
    assert "-p" in cmd  # print mode; the prompt goes on stdin, not argv
    assert cmd[cmd.index("--mode") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "plan-27b"
    assert cmd[cmd.index("--session-id") + 1]
    extension = Path(cmd[cmd.index("--extension") + 1])
    assert extension.name == "vllm_live_usage.mjs"
    assert extension.is_file()
    # The headless contract rides the SYSTEM prompt (re-presented every turn), so a weak model does
    # not drift into "what would you like me to do?" after a read-only tool turn.
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert "never ask a question" in sys_arg
    assert "EVERY turn" in sys_arg
    # `agent` is intentionally ignored (persona rides stdin, like opencode) — it must not leak into
    # argv via --append-system-prompt (the value is the fixed contract, not the phase id) or anywhere.
    assert "agent-plan" not in cmd
    assert "agent-plan" not in sys_arg
    assert "-a" in cmd
    # The fat prompt is fed on stdin (avoids the Windows argv limit + the @file hang).
    assert captured["input_text"] == "do it"
    assert captured["timeout"] == 90
    assert captured["cwd"] == str(tmp_path)
    assert captured["stream_path"] == stream  # the run-dir transcript file is forwarded
    assert callable(captured["should_stop"])


def test_pi_repair_reuses_the_exact_spawn_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import quill.runners.pi as pi_mod

    commands: list[list[str]] = []

    def fake_stream(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        commands.append(cmd)
        return "STREAMED STDOUT"

    monkeypatch.setattr(pi_mod, "run_streaming", fake_stream)
    monkeypatch.setattr(pi_mod.shutil, "which", lambda _name: "/usr/bin/pi")
    runner = PiRunner(directory=str(tmp_path))
    runner.spawn("plan", "gemma", "make plan", timeout=90, stream_path=tmp_path / "one.jsonl")
    runner.repair_session(
        "plan", "gemma", "write the file", timeout=90, stream_path=tmp_path / "two.jsonl"
    )

    first_session = commands[0][commands[0].index("--session-id") + 1]
    repair_session = commands[1][commands[1].index("--session-id") + 1]
    assert repair_session == first_session
    assert first_session


def test_pi_repair_without_a_matching_spawn_fails(tmp_path: Path) -> None:
    from quill.phases import SpawnError

    with pytest.raises(SpawnError, match="session id is unavailable"):
        PiRunner(directory=str(tmp_path)).repair_session(
            "plan", "gemma", "write it", timeout=90, stream_path=tmp_path / "repair.jsonl"
        )


def test_pi_spawn_nonzero_exit_raises_spawn_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import quill.runners.pi as pi_mod
    from quill.phases import SpawnError

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise SpawnError("'pi:a' spawn exited 1: boom")

    monkeypatch.setattr(pi_mod, "run_streaming", boom)
    monkeypatch.setattr(pi_mod.shutil, "which", lambda _name: "/usr/bin/pi")
    with pytest.raises(SpawnError, match="exited 1"):
        PiRunner(directory=str(tmp_path)).spawn(
            "a", "p", "x", timeout=5, stream_path=tmp_path / "s.jsonl"
        )


# -- preflight --------------------------------------------------------------------


def test_pi_preflight_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import quill.runners.pi as pi_mod

    monkeypatch.setattr(pi_mod.shutil, "which", lambda _name: None)
    with pytest.raises(PreflightError, match="pi was not found"):
        PiRunner(directory=".").preflight()


def test_pi_preflight_passes_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    import quill.runners.pi as pi_mod

    monkeypatch.setattr(pi_mod.shutil, "which", lambda _name: "/usr/bin/pi")
    PiRunner(directory=".").preflight()  # no raise


def test_opencode_preflight_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import quill.runners.opencode as oc_mod

    monkeypatch.setattr(oc_mod.shutil, "which", lambda _name: None)
    with pytest.raises(PreflightError, match="opencode was not found"):
        OpencodeRunner(directory=".").preflight()
