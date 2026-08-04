"""Workspace manager tests — the service's per-repo checkouts (server milestone B1)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from quill.git_ops import GitError, SubprocessRunner
from quill_api.workspace import (
    WorkspaceConflict,
    WorkspaceError,
    WorkspaceGitError,
    WorkspaceManager,
    WorkspaceNotFound,
    validate_branch,
    validate_repo,
)


class _FakeRunner:
    """Records commands per directory and answers the read-only git queries the manager makes.

    Branch existence is driven by ``remote_branches``/``local_branches``; ``current`` is what
    ``rev-parse`` reports; ``dirty`` makes ``git status --porcelain`` non-empty. Mutating commands
    are logged but do not change state — tests assert on the recorded command sequence.
    """

    def __init__(
        self,
        log: list[tuple[str, list[str]]],
        remote_branches: set[str],
        *,
        local_branches: set[str] | None = None,
        current: str = "some-branch",
        dirty: bool = False,
        rev_counts: str = "0 2",
        diffs: dict[str, str] | None = None,
        merge_base: str = "base-sha",
    ) -> None:
        self.log = log
        self.remote_branches = remote_branches
        self.local_branches = set(local_branches) if local_branches is not None else set()
        self.current = current
        self.dirty = dirty
        self.rev_counts = rev_counts
        self.diffs = diffs or {}
        self.merge_base = merge_base
        self.directory = ""
        self.fail_on: str | None = None

    def bind(self, directory: str) -> _FakeRunner:
        clone = _FakeRunner(
            self.log,
            self.remote_branches,
            local_branches=self.local_branches,
            current=self.current,
            dirty=self.dirty,
            rev_counts=self.rev_counts,
            diffs=self.diffs,
            merge_base=self.merge_base,
        )
        clone.directory = directory
        clone.fail_on = self.fail_on
        return clone

    def __call__(self, args: Sequence[str]) -> str:
        cmd = list(args)
        self.log.append((self.directory, cmd))
        joined = " ".join(cmd)
        if self.fail_on and self.fail_on in joined:
            raise GitError(f"boom: {joined}")
        if cmd[:3] == ["git", "ls-remote", "--heads"]:
            return f"sha\trefs/heads/{cmd[-1]}" if cmd[-1] in self.remote_branches else ""
        if cmd[:3] == ["git", "ls-remote", "--symref"]:
            return "ref: refs/heads/main\tHEAD\nsha\tHEAD"
        if cmd[:2] == ["git", "rev-parse"]:
            return self.current
        if cmd[:2] == ["git", "for-each-ref"]:
            prefix = cmd[-1]
            if prefix == "refs/heads":
                return "\n".join(f"refs/heads/{name}" for name in sorted(self.local_branches))
            if prefix == "refs/remotes/origin":
                lines = [f"refs/remotes/origin/{name}" for name in sorted(self.remote_branches)]
                # git always lists the symbolic origin/HEAD alias; the manager must drop it.
                lines.append("refs/remotes/origin/HEAD")
                return "\n".join(lines)
            return ""
        if cmd[:2] == ["git", "status"]:
            return "M tracked.txt\n?? untracked.txt" if self.dirty else ""
        if cmd[:2] == ["git", "rev-list"]:
            return self.rev_counts
        if cmd[:2] == ["git", "merge-base"]:
            return self.merge_base
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return self.diffs.get(cmd[-2], "feature.txt")
        if cmd[:3] == ["gh", "pr", "list"]:
            return "[]"
        if cmd[:3] == ["git", "show-ref", "--verify"]:
            branch = cmd[-1].removeprefix("refs/heads/")
            if branch in self.local_branches:
                return ""
            raise GitError(f"not a valid ref refs/heads/{branch}")
        return ""


def _manager(
    tmp_path: Path,
    *,
    remote_branches: set[str] | None = None,
    local_branches: set[str] | None = None,
    current: str = "some-branch",
    dirty: bool = False,
    fail_on: str | None = None,
    rev_counts: str = "0 2",
    diffs: dict[str, str] | None = None,
    merge_base: str = "base-sha",
    git_author: tuple[str, str] | None = None,
) -> tuple[WorkspaceManager, list[tuple[str, list[str]]]]:
    log: list[tuple[str, list[str]]] = []
    template = _FakeRunner(
        log,
        remote_branches if remote_branches is not None else {"main"},
        local_branches=local_branches,
        current=current,
        dirty=dirty,
        rev_counts=rev_counts,
        diffs=diffs,
        merge_base=merge_base,
    )
    template.fail_on = fail_on
    return (
        WorkspaceManager(tmp_path / "ws", runner_factory=template.bind, git_author=git_author),
        log,
    )


def _commands(log: list[tuple[str, list[str]]]) -> list[str]:
    return [" ".join(cmd) for _dir, cmd in log]


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# -- validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "../../etc",
        "owner/../../etc",
        "owner",
        "owner/name/extra",
        "own er/name",
        "owner/name; rm -rf /",
        "",
        "/absolute/path",
    ],
)
def test_invalid_repos_are_refused(repo: str) -> None:
    """`repo` becomes both a path segment and an argv entry, so it is validated before either."""
    with pytest.raises(WorkspaceError, match="invalid repo"):
        validate_repo(repo)


def test_valid_repo_accepted() -> None:
    assert validate_repo("  enisbukalo/Workbench  ") == "enisbukalo/Workbench"


@pytest.mark.parametrize(
    "branch",
    ["--upload-pack=evil", "has space", "with..dots", "ref@{0}", "", "trailing/", "x.lock", "sub."],
)
def test_invalid_branches_are_refused(branch: str) -> None:
    with pytest.raises(WorkspaceError, match="invalid branch"):
        validate_branch(branch)


@pytest.mark.parametrize("branch", ["main", "ticket-42-fix", "feature/thing", "v1.2.3", "a_b"])
def test_valid_branches_accepted(branch: str) -> None:
    assert validate_branch(branch) == branch


def test_a_leading_dash_branch_cannot_become_a_git_flag() -> None:
    with pytest.raises(WorkspaceError):
        validate_branch("--exec=whoami")


# -- prepare ----------------------------------------------------------------------


def test_prepare_clones_a_repo_it_has_never_seen(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path)

    workspace = manager.prepare("me/proj", "main", base="main")

    assert workspace.path == tmp_path / "ws" / "me" / "proj"
    assert any(cmd.startswith("gh repo clone me/proj") for cmd in _commands(log))


def test_prepare_reuses_an_existing_checkout(tmp_path: Path) -> None:
    """The clone is persistent — re-cloning would discard the build cache it exists to keep."""
    manager, log = _manager(tmp_path)
    path = manager.path_for("me/proj")
    (path / ".git").mkdir(parents=True)

    manager.prepare("me/proj", "main", base="main")

    assert not any("repo clone" in cmd for cmd in _commands(log))
    assert "git fetch origin --prune" in _commands(log)


def test_restart_status_requires_a_local_ahead_clean_lineage(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, local_branches={"feature_1"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.restart_status("me/proj", "feature_1", base="main")

    assert result.eligible is True
    assert result.ahead == 2
    commands = _commands(log)
    assert "git rev-list --left-right --count origin/main...feature_1" in commands
    assert any(
        command.startswith("gh pr list --repo me/proj --head feature_1") for command in commands
    )


def test_restart_status_allows_non_overlapping_base_advances(tmp_path: Path) -> None:
    manager, _log = _manager(
        tmp_path,
        local_branches={"feature_1"},
        rev_counts="1 2",
        diffs={
            "origin/main...feature_1": "feature.gd",
            "base-sha..origin/main": "tests/runner.gd",
        },
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.restart_status("me/proj", "feature_1", base="main", checkpoint_base="base-sha")

    assert result.eligible is True
    assert result.ahead == 2
    assert result.behind == 1


def test_restart_status_blocks_overlapping_base_advances(tmp_path: Path) -> None:
    manager, _log = _manager(
        tmp_path,
        local_branches={"feature_1"},
        rev_counts="1 2",
        diffs={
            "origin/main...feature_1": "shared.gd\nfeature.gd",
            "base-sha..origin/main": "shared.gd\ntests/runner.gd",
        },
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.restart_status("me/proj", "feature_1", base="main", checkpoint_base="base-sha")

    assert result.eligible is False
    assert result.reason == "origin/main advanced across file(s) changed by the run: shared.gd"


def test_restore_checkpoint_merges_a_non_overlapping_advanced_base(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    root = tmp_path / "ws"
    repo = root / "me" / "proj"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    checkpoint_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    _git(repo, "switch", "-c", "feature_1")
    (repo / "feature.gd").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature")
    checkpoint = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "main")
    (repo / "tests").mkdir()
    (repo / "tests" / "runner.gd").write_text("runner fix\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "runner fix")
    _git(repo, "push", "origin", "main")
    _git(repo, "switch", "feature_1")

    def runner_factory(directory: str):  # type: ignore[no-untyped-def]
        git = SubprocessRunner(directory)

        def run(args: Sequence[str]) -> str:
            if list(args)[:3] == ["gh", "pr", "list"]:
                return "[]"
            return git(args)

        return run

    manager = WorkspaceManager(root, runner_factory=runner_factory)
    status = manager.restart_status(
        "me/proj", "feature_1", base="main", checkpoint_base=checkpoint_base
    )

    assert status.eligible is True
    manager.restore_run_checkpoint("me/proj", "feature_1", checkpoint, base="main")

    assert (repo / "feature.gd").read_text(encoding="utf-8") == "feature\n"
    assert (repo / "tests" / "runner.gd").read_text(encoding="utf-8") == "runner fix\n"
    assert _git(repo, "rev-list", "--left-right", "--count", "origin/main...HEAD").split()[0] == "0"


def test_restore_checkpoint_rolls_back_when_base_merge_fails(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        local_branches={"feature_1"},
        current="original-sha",
        fail_on="merge --no-edit",
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceGitError, match="could not restore checkpoint"):
        manager.restore_run_checkpoint("me/proj", "feature_1", "checkpoint-sha", base="main")

    commands = _commands(log)
    assert "git merge --abort" in commands
    assert commands[-2:] == ["git reset --hard original-sha", "git clean -fd"]


def test_prepare_checks_out_an_existing_remote_branch(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main", "ticket-42"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    manager.prepare("me/proj", "ticket-42", base="main")

    commands = _commands(log)
    assert "git checkout -B ticket-42 origin/ticket-42" in commands
    assert "git reset --hard origin/ticket-42" in commands


def test_prepare_creates_a_missing_branch_from_base(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    manager.prepare("me/proj", "brand-new", base="main")

    commands = _commands(log)
    assert "git checkout -B brand-new origin/main" in commands
    assert "git reset --hard origin/main" in commands


def test_prepare_fails_when_neither_branch_nor_base_exists(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path, remote_branches=set())
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="neither branch"):
        manager.prepare("me/proj", "nope", base="also-nope")


def test_prepare_cleans_untracked_but_keeps_ignored_files(tmp_path: Path) -> None:
    """`-fd` not `-fdx`: ignored files are build caches, and a cold C++ rebuild every run is the
    thing this is avoiding."""
    manager, log = _manager(tmp_path)
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    manager.prepare("me/proj", "main", base="main")

    commands = _commands(log)
    assert "git clean -fd" in commands
    assert "git clean -fdx" not in commands


def test_prepare_wraps_git_failures(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path, fail_on="fetch")
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="could not prepare me/proj"):
        manager.prepare("me/proj", "main", base="main")


def test_prepare_validates_before_touching_the_filesystem(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path)

    with pytest.raises(WorkspaceError):
        manager.prepare("../../etc", "main", base="main")

    assert log == []
    assert not (tmp_path / "ws").exists()


def test_prepare_for_config_uses_existing_requested_branch(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main", "feature"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    prepared = manager.prepare_for_config("me/proj", "feature")

    assert prepared.requested_branch_exists is True
    assert prepared.workspace.branch == "feature"
    assert "git checkout -B feature origin/feature" in _commands(log)
    assert not any("--symref" in command for command in _commands(log))


def test_prepare_for_config_uses_default_branch_when_requested_is_new(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    prepared = manager.prepare_for_config("me/proj", "feature")

    assert prepared.requested_branch_exists is False
    assert prepared.workspace.branch == "main"
    assert "git ls-remote --symref origin HEAD" in _commands(log)
    assert "git checkout -B main origin/main" in _commands(log)


def test_prepare_for_config_reports_unknown_default_branch(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path, fail_on="--symref")
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="default branch"):
        manager.prepare_for_config("me/proj", "feature")


def test_prepare_default_for_config_ignores_existing_feature_branch(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main", "feature"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    prepared = manager.prepare_default_for_config("me/proj")

    assert prepared.workspace.branch == "main"
    assert prepared.requested_branch_exists is False
    assert "git checkout -B main origin/main" in _commands(log)


# -- listing ----------------------------------------------------------------------


def test_checkouts_is_empty_before_anything_is_cloned(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path)
    assert manager.checkouts() == []


def test_checkouts_reports_each_one(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path)
    for repo in ("me/one", "you/two"):
        (manager.path_for(repo) / ".git").mkdir(parents=True)

    listed = manager.checkouts()

    assert [w.repo for w in listed] == ["me/one", "you/two"]
    assert all(w.branch == "some-branch" for w in listed)


# -- branch listing ---------------------------------------------------------------


def test_branches_requires_an_existing_checkout(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path)
    with pytest.raises(WorkspaceNotFound, match="no checkout"):
        manager.branches("me/proj")


def test_branches_merges_local_and_remote_and_marks_current(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"main", "feature", "shared"},
        local_branches={"main", "local-only", "shared"},
        current="feature",
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    branches = manager.branches("me/proj")

    assert "git fetch origin --prune" in _commands(log)
    by_name = {b.name: b for b in branches}
    # A branch on both sides is deduplicated to one entry flagged local+remote.
    assert by_name["shared"].local and by_name["shared"].remote
    # Remote-only and local-only branches are both offered, with the right flags.
    assert by_name["feature"].remote and not by_name["feature"].local
    assert by_name["local-only"].local and not by_name["local-only"].remote
    # The symbolic origin/HEAD alias is never surfaced as a branch.
    assert "HEAD" not in by_name


def test_branches_sorts_current_first_then_lexicographically(tmp_path: Path) -> None:
    manager, _log = _manager(
        tmp_path,
        remote_branches={"main", "alpha", "zeta"},
        local_branches={"main", "alpha", "zeta"},
        current="zeta",
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    names = [b.name for b in manager.branches("me/proj")]

    assert names[0] == "zeta"
    assert names[1:] == ["alpha", "main"]
    assert manager.branches("me/proj")[0].current is True


def test_branches_wraps_git_failures(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path, fail_on="for-each-ref")
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceGitError, match="could not list branches"):
        manager.branches("me/proj")


def test_branches_rejects_an_invalid_repo(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path)
    with pytest.raises(WorkspaceError, match="invalid repo"):
        manager.branches("../../etc")


# -- pull -------------------------------------------------------------------------


def test_pull_fast_forwards_an_existing_local_branch(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path, remote_branches={"main", "feature"}, local_branches={"main", "feature"}
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.pull_branch("me/proj", "feature")

    commands = _commands(log)
    assert "git checkout feature" in commands
    assert "git pull --ff-only origin feature" in commands
    assert result.branch == "feature"


def test_pull_checks_out_a_remote_only_branch_tracking_origin(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main", "feature"}, local_branches={"main"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    manager.pull_branch("me/proj", "feature")

    commands = _commands(log)
    assert "git checkout -b feature --track origin/feature" in commands
    assert "git pull --ff-only origin feature" in commands


def test_pull_refuses_a_branch_missing_on_origin(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path, remote_branches={"main"}, local_branches={"main", "stale"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceNotFound, match="no branch 'stale' on origin"):
        manager.pull_branch("me/proj", "stale")


def test_pull_refuses_a_dirty_worktree_without_mutating(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, remote_branches={"main"}, local_branches={"main"}, dirty=True)
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceConflict, match="uncommitted changes"):
        manager.pull_branch("me/proj", "main")

    assert "git pull --ff-only origin main" not in _commands(log)


def test_pull_reports_divergence_as_a_conflict(tmp_path: Path) -> None:
    manager, _log = _manager(
        tmp_path, remote_branches={"main"}, local_branches={"main"}, fail_on="pull --ff-only"
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceConflict, match="could not fast-forward"):
        manager.pull_branch("me/proj", "main")


def test_pull_requires_an_existing_checkout(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path)
    with pytest.raises(WorkspaceNotFound, match="no checkout"):
        manager.pull_branch("me/proj", "main")


def test_pull_rejects_an_invalid_branch(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path)
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="invalid branch"):
        manager.pull_branch("me/proj", "--exec=whoami")


# -- delete -----------------------------------------------------------------------


def test_delete_removes_a_non_current_local_branch_only(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"main", "feature"},
        local_branches={"main", "feature"},
        current="main",
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.delete_branch("me/proj", "feature")

    commands = _commands(log)
    assert "git branch -D feature" in commands
    # Only the local ref is removed — origin is never touched, and HEAD does not move.
    assert not any(cmd.startswith("git push") for cmd in commands)
    assert "git checkout -B main origin/main" not in commands
    assert result.branch == "main"
    assert "preserved" in result.message


def test_delete_of_current_branch_switches_to_default_first(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"main", "feature"},
        local_branches={"main", "feature"},
        current="feature",
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.delete_branch("me/proj", "feature")

    commands = _commands(log)
    # Switch to the remote default before deleting the branch HEAD was on.
    assert commands.index("git checkout -B main origin/main") < commands.index(
        "git branch -D feature"
    )
    assert result.branch == "main"


def test_delete_refuses_the_default_branch(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path, remote_branches={"main"}, local_branches={"main"}, current="main"
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceConflict, match="default branch and cannot be deleted"):
        manager.delete_branch("me/proj", "main")

    assert "git branch -D main" not in _commands(log)


def test_delete_refuses_a_missing_local_branch(tmp_path: Path) -> None:
    manager, _log = _manager(tmp_path, remote_branches={"main", "feature"}, local_branches={"main"})
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceNotFound, match="no local branch 'feature'"):
        manager.delete_branch("me/proj", "feature")


def test_delete_of_current_branch_refuses_a_dirty_worktree(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"main", "feature"},
        local_branches={"main", "feature"},
        current="feature",
        dirty=True,
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceConflict, match="uncommitted changes"):
        manager.delete_branch("me/proj", "feature")

    assert "git branch -D feature" not in _commands(log)


# -- failed-run cleanup -----------------------------------------------------------


def test_discard_run_branch_drops_changes_switches_to_main_and_deletes_local(
    tmp_path: Path,
) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"main", "feature"},
        local_branches={"main", "feature"},
        current="feature",
        dirty=True,
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    result = manager.discard_run_branch("me/proj", "feature")

    commands = _commands(log)
    assert commands.index("git reset --hard") < commands.index("git checkout -B main origin/main")
    assert commands.count("git clean -fd") == 2
    assert commands.index("git checkout -B main origin/main") < commands.index(
        "git branch -D feature"
    )
    assert not any(command.startswith("git push") for command in commands)
    assert result.branch == "main"


def test_discard_run_on_main_cleans_and_resets_without_deleting_main(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"main"},
        local_branches={"main"},
        current="main",
        dirty=True,
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    manager.discard_run_branch("me/proj", "main")

    commands = _commands(log)
    assert "git checkout -B main origin/main" in commands
    assert "git reset --hard origin/main" in commands
    assert "git branch -D main" not in commands


def test_discard_run_branch_requires_main_on_origin(tmp_path: Path) -> None:
    manager, log = _manager(
        tmp_path,
        remote_branches={"feature"},
        local_branches={"feature"},
        current="feature",
    )
    (manager.path_for("me/proj") / ".git").mkdir(parents=True)

    with pytest.raises(WorkspaceNotFound, match="no 'main' branch"):
        manager.discard_run_branch("me/proj", "feature")

    assert "git branch -D feature" not in _commands(log)


# -- serialization ----------------------------------------------------------------


def test_same_repo_shares_one_lock_and_different_repos_do_not(tmp_path: Path) -> None:
    """One RLock per repo: same-repo operations serialise, different repos stay independent."""
    manager, _log = _manager(tmp_path)
    assert manager._lock_for("me/proj") is manager._lock_for("me/proj")
    assert manager._lock_for("me/proj") is not manager._lock_for("you/other")


def test_prepare_stamps_commit_identity_on_a_fresh_clone(tmp_path: Path) -> None:
    """A new checkout must never inherit the service user's global git identity."""
    manager, log = _manager(tmp_path, git_author=("agent", "agent@users.noreply.github.com"))

    manager.prepare("me/proj", "main", base="main")

    commands = _commands(log)
    assert "git config user.name agent" in commands
    assert "git config user.email agent@users.noreply.github.com" in commands


def test_prepare_restamps_identity_on_an_existing_checkout(tmp_path: Path) -> None:
    """Checkouts cloned before the setting existed keep committing under the old identity
    unless it is re-applied every time, so this must not be clone-only."""
    manager, log = _manager(tmp_path, git_author=("agent", "agent@users.noreply.github.com"))
    path = manager.path_for("me/proj")
    (path / ".git").mkdir(parents=True)

    manager.prepare("me/proj", "main", base="main")

    commands = _commands(log)
    assert not any(c.startswith("gh repo clone") for c in commands), "should not re-clone"
    assert "git config user.name agent" in commands
    assert "git config user.email agent@users.noreply.github.com" in commands


def test_prepare_without_configured_author_leaves_identity_alone(tmp_path: Path) -> None:
    manager, log = _manager(tmp_path, git_author=None)

    manager.prepare("me/proj", "main", base="main")

    assert not any(c.startswith("git config user.") for c in _commands(log))
