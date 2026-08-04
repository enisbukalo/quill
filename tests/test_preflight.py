"""gh CLI preflight tests (WI-13) — gh calls mocked, no live gh dependency."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quill import preflight
from quill.preflight import (
    PreflightError,
    check_gh,
    check_target_dir,
    gh_authenticated,
    gh_available,
    gh_version,
)


def _proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr="")


def test_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_run_gh", lambda *a: _proc(0, "gh version 2.83.1\n"))
    assert gh_available() is True


def test_available_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_run_gh", lambda *a: None)  # binary not on PATH
    assert gh_available() is False


def test_available_false_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_run_gh", lambda *a: _proc(1))
    assert gh_available() is False


def test_authenticated_reflects_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_run_gh", lambda *a: _proc(0))
    assert gh_authenticated() is True
    monkeypatch.setattr(preflight, "_run_gh", lambda *a: _proc(1))
    assert gh_authenticated() is False


def test_version_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight, "_run_gh", lambda *a: _proc(0, "gh version 2.83.1 (2025-11-13)\nmore\n")
    )
    assert gh_version() == "gh version 2.83.1 (2025-11-13)"


def test_version_none_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_run_gh", lambda *a: None)
    assert gh_version() is None


def test_check_gh_passes_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "gh_available", lambda: True)
    monkeypatch.setattr(preflight, "gh_authenticated", lambda: True)
    check_gh()  # no raise


def test_check_gh_missing_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "gh_available", lambda: False)
    with pytest.raises(PreflightError, match="not found on PATH"):
        check_gh()


def test_check_gh_unauthenticated_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "gh_available", lambda: True)
    monkeypatch.setattr(preflight, "gh_authenticated", lambda: False)
    with pytest.raises(PreflightError, match="not authenticated"):
        check_gh()


def test_run_gh_missing_binary_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> object:
        raise OSError("no gh")

    monkeypatch.setattr(subprocess, "run", boom)
    assert preflight._run_gh("--version") is None


# -- check_target_dir -------------------------------------------------------------


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True, check=True)


def test_target_dir_missing(tmp_path: Path) -> None:
    with pytest.raises(PreflightError, match="does not exist"):
        check_target_dir(str(tmp_path / "nope"))


def test_target_dir_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(PreflightError, match="not a git repository"):
        check_target_dir(str(tmp_path))


def test_target_dir_repo_without_remote(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    with pytest.raises(PreflightError, match="no 'origin' remote"):
        check_target_dir(str(tmp_path))


def test_target_dir_ok(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/me/proj.git")
    check_target_dir(str(tmp_path))  # no raise


# -- check_opencode / check_router ------------------------------------------------


def test_opencode_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)
    with pytest.raises(PreflightError, match="opencode was not found"):
        preflight.check_opencode()


def test_opencode_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "/usr/bin/opencode")
    preflight.check_opencode()  # no raise


def test_router_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> object:
        raise preflight.httpx.ConnectError("refused")

    monkeypatch.setattr(preflight.httpx, "get", boom)
    with pytest.raises(PreflightError, match="router is not reachable"):
        preflight.check_router("http://localhost:8001")


def test_router_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def raise_for_status(self) -> None: ...

    monkeypatch.setattr(preflight.httpx, "get", lambda *a, **k: _Resp())
    preflight.check_router("http://localhost:8001")  # no raise


def test_vllm_default_checks_health_without_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None: ...

    monkeypatch.setattr(preflight.httpx, "get", lambda url, **_kwargs: calls.append(url) or _Resp())
    monkeypatch.setattr(
        preflight.httpx, "post", lambda url, **_kwargs: calls.append(url) or _Resp()
    )
    preflight.check_vllm("http://vllm")
    assert calls == ["http://vllm/health"]


def test_vllm_cold_run_probes_reset_route(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None: ...

    monkeypatch.setattr(preflight.httpx, "get", lambda url, **_kwargs: calls.append(url) or _Resp())
    monkeypatch.setattr(
        preflight.httpx, "post", lambda url, **_kwargs: calls.append(url) or _Resp()
    )
    preflight.check_vllm("http://vllm", clear_prefix_cache=True)
    assert calls == ["http://vllm/health", "http://vllm/reset_prefix_cache"]
