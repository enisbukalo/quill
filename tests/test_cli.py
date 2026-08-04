"""CLI arg-guard + flow tests (ticket #33). Preflight + pipeline mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill import cli
from quill import config as cfg
from quill.bootstrap import init_config, seed_personas


@pytest.fixture(autouse=True)
def _stub_git_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_detect_repo", lambda _d: "me/proj")
    monkeypatch.setattr(cfg, "_detect_default_branch", lambda _d: "main")


def _filled_vault(directory: Path) -> None:
    config_file = init_config(directory)
    seed_personas()
    text = config_file.read_text(encoding="utf-8")
    text = text.replace('kind = ""', 'kind = "opencode"')
    text = text.replace('command = ""', 'command = "make"')
    text = text.replace('test    = ""', 'test    = "make test"')
    config_file.write_text(text, encoding="utf-8")


def _raise() -> None:
    from quill.preflight import PreflightError

    raise PreflightError("gh down (test)")


# -- arg guards -------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_non_positive_ticket_rejected(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main([bad])
    assert rc == 2
    assert "ticket must be a positive issue number" in capsys.readouterr().err


def test_no_ticket_no_init_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main([])
    assert rc == 2
    assert "ticket number is required" in capsys.readouterr().err


# -- --init -----------------------------------------------------------------------


def test_init_writes_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["--init"])
    assert rc == 0
    assert (tmp_path / cfg.CONFIG_FILENAME).exists()
    # --init also seeds the machine-level persona library, so the default config is runnable.
    assert (cfg.default_personas_root() / "plan.md").is_file()


def test_init_refuses_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    init_config(tmp_path)
    rc = cli.main(["--init"])
    assert rc == 2


# -- missing vault tells user to init ---------------------------------------------


def test_missing_vault_points_at_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_gh", lambda: None)
    monkeypatch.setattr(cli, "check_target_dir", lambda _d: None)
    rc = cli.main(["42"])
    assert rc == 2
    assert "quill --init" in capsys.readouterr().err


# -- --start-phase is a phase id --------------------------------------------------


def test_start_phase_unknown_id_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _filled_vault(tmp_path)
    monkeypatch.setattr(cli, "check_gh", lambda: None)
    monkeypatch.setattr(cli, "check_target_dir", lambda _d: None)
    rc = cli.main(["42", "--start-phase", "phase99"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a configured phase id" in err
    assert "plan" in err  # lists the valid ids


def test_start_phase_valid_id_passes_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid --start-phase clears the arg guard and reaches runner preflight (mocked fail)."""
    monkeypatch.chdir(tmp_path)
    _filled_vault(tmp_path)
    monkeypatch.setattr(cli, "check_gh", lambda: None)
    monkeypatch.setattr(cli, "check_target_dir", lambda _d: None)

    class _Runner:
        def preflight(self) -> None:
            _raise()

    monkeypatch.setattr(cli, "get_runner", lambda *_a, **_k: _Runner())
    rc = cli.main(["42", "--start-phase", "plan"])
    assert rc == 2  # stopped at runner preflight, not the arg guard


# -- --resume ---------------------------------------------------------------------


def test_resume_no_state_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _filled_vault(tmp_path)
    monkeypatch.setattr(cli, "check_gh", lambda: None)
    monkeypatch.setattr(cli, "check_target_dir", lambda _d: None)
    rc = cli.main(["42", "--resume"])
    assert rc == 2
    assert "nothing to resume" in capsys.readouterr().err


def test_resume_ticket_mismatch_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from quill import events
    from quill.config import load_config
    from quill.runstate_file import make_recorder

    monkeypatch.chdir(tmp_path)
    _filled_vault(tmp_path)
    config = load_config(str(tmp_path))
    rec = make_recorder(config, ticket=99, run_id="run-A", base_on_event=lambda _e: None)
    rec(events.phase_started("plan", "plan"))
    rec(events.run_halted(reason="x", phase="plan"))

    monkeypatch.setattr(cli, "check_gh", lambda: None)
    monkeypatch.setattr(cli, "check_target_dir", lambda _d: None)
    rc = cli.main(["42", "--resume"])
    assert rc == 2
    assert "for ticket 99, not 42" in capsys.readouterr().err


# -- --update ---------------------------------------------------------------------


def test_update_and_resume_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--resume replays a saved run; --update starts a fresh one primed with PR feedback.

    Honouring both would resume a run that never saw the feedback, so reject the combination
    before any preflight work happens.
    """
    rc = cli.main(["42", "--update", "--resume"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_update_passes_update_mode_to_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`quill 42 --update` reaches run_pipeline with mode="update" (plain runs stay "create")."""
    monkeypatch.chdir(tmp_path)
    _filled_vault(tmp_path)
    monkeypatch.setattr(cli, "check_gh", lambda: None)
    monkeypatch.setattr(cli, "check_target_dir", lambda _d: None)
    monkeypatch.setattr(cli, "_check_backend", lambda _c, **_k: None)

    class _Runner:
        def preflight(self) -> None: ...
        def spawn(self, *a: object, **k: object) -> str:
            return ""

        def extract_receipt(self, stdout: str) -> str | None:
            return None

        def skill_directive(self, names: list[str]) -> str:
            return ""

    class _Loader:
        def load(self, preset: str, timeout: float = 180) -> None: ...
        def unload_all(self) -> None: ...

    monkeypatch.setattr(cli, "get_runner", lambda *_a, **_k: _Runner())
    monkeypatch.setattr(cli, "make_model_server", lambda _c, **_k: _Loader())

    seen: dict[str, object] = {}

    def fake_pipeline(ticket: int, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"type": "run_done"}

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)

    assert cli.main(["42", "--update"]) == 0
    assert seen["mode"] == "update"

    seen.clear()
    assert cli.main(["42"]) == 0
    assert seen["mode"] == "create"


# -- remote mode ------------------------------------------------------------------


def test_server_flag_drives_the_remote_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --server the local preflight (gh, models, a git remote) must not run at all — the
    server owns that stack, which is the whole point of remote mode."""
    seen: dict[str, object] = {}

    def fake_run_remote(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"type": "run_done"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("quill.client.run_remote", fake_run_remote)

    assert cli.main(["42", "--server", "http://box:8002"]) == 0
    assert seen["server"] == "http://box:8002"
    assert seen["ticket"] == 42


def test_server_flag_reads_quill_server_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUILL_SERVER", "http://from-env")
    seen: dict[str, object] = {}

    def fake_run_remote(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"type": "run_done"}

    monkeypatch.setattr("quill.client.run_remote", fake_run_remote)

    assert cli.main(["42"]) == 0
    assert seen["server"] == "http://from-env"


@pytest.mark.parametrize("flag", [["--resume"], ["--start-phase", "impl"]])
def test_local_only_flags_are_refused_with_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: list
) -> None:
    """Both replay state this machine holds; a remote run has none of it here."""
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["42", "--server", "http://box", *flag])
    assert rc == 2
    assert "local-only" in capsys.readouterr().err


def test_remote_failure_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from quill.client import ClientError

    def boom(**_kwargs: object) -> dict[str, object]:
        raise ClientError("could not reach http://box")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("quill.client.run_remote", boom)

    assert cli.main(["42", "--server", "http://box"]) == 2
    assert "could not reach" in capsys.readouterr().err
