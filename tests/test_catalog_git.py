"""Catalog git writes — commit messages and the graded failure policy.

Driven against real throwaway repositories with a local bare "remote", because the behaviour under
test *is* git's: what a rejected push looks like, what a rebase conflict does to a commit. A fake
runner would only assert that we call the commands we already decided to call.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from quill.git_ops import GitError, SubprocessRunner
from quill_api.catalog_git import CatalogConflict, CatalogRepo, commit_message


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare repo standing in for the config repo's origin."""
    path = tmp_path / "remote.git"
    path.mkdir()
    _git(path, "init", "--bare", "--initial-branch=main")
    return path


@pytest.fixture
def library(tmp_path: Path, remote: Path) -> Path:
    """A clone with one committed persona, tracking the bare remote."""
    path = tmp_path / "library"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "quill@test")
    _git(path, "config", "user.name", "quill")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "plan.md").write_text("original", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "seed")
    _git(path, "remote", "add", "origin", str(remote))
    _git(path, "push", "-u", "origin", "main")
    return path


def _last_commit_message(path: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


# -- message shape ----------------------------------------------------------------


def test_commit_message_carries_the_reason() -> None:
    message = commit_message("skills", "cpp-pro", "update", "SKILL.md", "add move semantics")
    assert message == "skills(cpp-pro): update SKILL.md — add move semantics"


# -- the happy path ---------------------------------------------------------------


def test_a_write_is_committed_and_pushed(library: Path, remote: Path) -> None:
    (library / "plan.md").write_text("edited", encoding="utf-8")
    repo = CatalogRepo(library)

    result = repo.commit_and_push([library / "plan.md"], "personas(plan): update plan.md — why")

    assert result.committed and result.pushed
    assert result.sha
    assert _last_commit_message(library) == "personas(plan): update plan.md — why"
    # It really reached the remote, not just local history.
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%B", "main"],
        cwd=remote,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "update plan.md" in log


def test_the_reason_is_the_audit_trail(library: Path) -> None:
    """With no auth on the API, `git log` is the only record of who changed a shared persona."""
    (library / "plan.md").write_text("edited", encoding="utf-8")

    CatalogRepo(library).commit_and_push(
        [library / "plan.md"], commit_message("personas", "plan", "update", "plan.md", "tightened")
    )

    assert "tightened" in _last_commit_message(library)


# -- graded failures --------------------------------------------------------------


def test_an_unchanged_file_is_not_committed(library: Path) -> None:
    """Rewriting a file with identical content is a no-op, not a failure."""
    result = CatalogRepo(library).commit_and_push([library / "plan.md"], "personas(plan): noop")

    assert not result.committed
    assert result.error is not None and "no change" in result.error


def test_a_failed_push_still_reports_the_commit(library: Path) -> None:
    """A network blip must not be reported as a failed edit — the change is safe in local
    history, so the request succeeds with pushed=False."""
    (library / "plan.md").write_text("edited", encoding="utf-8")
    _git(library, "remote", "set-url", "origin", "/nonexistent/remote.git")

    result = CatalogRepo(library).commit_and_push([library / "plan.md"], "personas(plan): update")

    assert result.committed
    assert not result.pushed
    assert result.error
    assert _last_commit_message(library) == "personas(plan): update"


def test_a_diverged_remote_raises_conflict_with_the_commit_intact(
    library: Path, remote: Path, tmp_path: Path
) -> None:
    """The config repo is edited from several machines, so divergence is expected. The commit
    stands; only the rebase fails, and a human resolves it."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "clone", str(remote), ".")
    _git(other, "config", "user.email", "other@test")
    _git(other, "config", "user.name", "other")
    (other / "plan.md").write_text("their edit", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "their change")
    _git(other, "push")

    (library / "plan.md").write_text("my edit", encoding="utf-8")

    with pytest.raises(CatalogConflict):
        CatalogRepo(library).commit_and_push([library / "plan.md"], "personas(plan): mine")

    # The local commit survived the failed rebase — nothing was lost.
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%B"],
        cwd=library,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "personas(plan): mine" in log


def test_a_plain_directory_is_not_an_error(tmp_path: Path) -> None:
    """The libraries work perfectly well as plain directories; they just have no history."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "plan.md").write_text("body", encoding="utf-8")

    result = CatalogRepo(plain).commit_and_push([plain / "plan.md"], "personas(plan): update")

    assert not result.committed
    assert result.error is not None and "not a git repository" in result.error


def test_nothing_to_commit_is_reported(library: Path) -> None:
    result = CatalogRepo(library).commit_and_push([], "personas(x): update")
    assert not result.committed
    assert result.error is not None and "nothing to commit" in result.error


def test_a_staging_failure_raises(library: Path) -> None:
    """A write that cannot even be staged must fail the request rather than half-apply."""
    real = SubprocessRunner(directory=str(library))

    def fail_on_add(args: Sequence[str]) -> str:
        if list(args)[:2] == ["git", "add"]:
            raise GitError("git add exploded")
        return real(args)

    repo = CatalogRepo(library, runner=fail_on_add)
    with pytest.raises(RuntimeError, match="could not stage"):
        repo.commit_and_push([library / "plan.md"], "personas(plan): update")
