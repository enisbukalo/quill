"""Git-level tests for deterministic Quill commit attribution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from quill.attribution import commit_attribution


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=directory, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_commit_attribution_adds_trailers_and_restores_hook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo.mkdir()
    run_dir.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    hook = repo / ".git" / "hooks" / "commit-msg"
    original = b"#!/bin/sh\nexit 0\n"
    hook.write_bytes(original)
    hook.chmod(0o755)
    (repo / "change.txt").write_text("changed", encoding="utf-8")
    _git(repo, "add", "change.txt")

    with commit_attribution(repo, run_dir, "gemma-3"):
        _git(repo, "commit", "-m", "Implement change")

    message = _git(repo, "log", "-1", "--pretty=%B")
    assert "Generated-by: Quill" in message
    assert "Model: gemma-3" in message
    assert hook.read_bytes() == original
    assert not (run_dir / "commit-msg.previous").exists()
