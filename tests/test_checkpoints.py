"""Durable local Git boundaries for phase restarts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from quill.checkpoints import CheckpointRecorder, load_manifest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "--initial-branch=main", str(repo))
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example", "commit", "-m", "seed")
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "checkout", "-b", "feature_1")
    return repo, remote


def test_records_latest_boundary_and_terminal_work(tmp_path: Path) -> None:
    repo, _remote = _repo(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    recorder = CheckpointRecorder(
        repo,
        run_dir,
        run_id="r1",
        repo="me/repo",
        branch="feature_1",
        base_branch="main",
        phases=("plan", "impl", "review"),
    )

    plan_checkpoint = recorder.before_phase("plan")
    assert plan_checkpoint == _git(repo, "rev-parse", "HEAD")
    (repo / "feature.txt").write_text("first\n", encoding="utf-8")
    impl_checkpoint = recorder.before_phase("impl")
    first_impl = load_manifest(run_dir)
    assert first_impl is not None
    assert first_impl.commit_for("impl") == _git(repo, "rev-parse", "HEAD")
    assert impl_checkpoint == first_impl.commit_for("impl")

    (repo / "feature.txt").write_text("second\n", encoding="utf-8")
    assert recorder.recover_terminal("impl") is True
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "diff", "--name-only", "origin/main...HEAD") == "feature.txt"
    assert _git(repo, "show-ref", "--verify", "refs/quill/runs/r1")


def test_delivery_removes_checkpoint_commits_but_keeps_changes(tmp_path: Path) -> None:
    repo, _remote = _repo(tmp_path)
    run_dir = tmp_path / "runs" / "r2"
    recorder = CheckpointRecorder(
        repo,
        run_dir,
        run_id="r2",
        repo="me/repo",
        branch="feature_1",
        base_branch="main",
        phases=("impl", "commit"),
    )
    (repo / "feature.txt").write_text("work\n", encoding="utf-8")

    recorder.before_phase("impl")
    recorder.before_phase("commit")

    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    assert "feature.txt" in _git(repo, "status", "--porcelain")
    manifest = load_manifest(run_dir)
    assert manifest is not None
    checkpoint = manifest.commit_for("commit")
    assert checkpoint is not None
    assert _git(repo, "cat-file", "-t", checkpoint) == "commit"
