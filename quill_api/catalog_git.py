"""Commit and push catalog edits to the repo the libraries live in (server milestone C).

Personas and skills are checked into a config repo and symlinked into place, so editing one over
HTTP is editing a git working tree. Every write is committed and pushed with the ``reason`` the
caller had to supply, which makes ``git log`` the audit trail: without it there would be no record
of who changed a shared persona or why.

The failure policy is graded, because these outcomes are not equally recoverable:

* **write or commit fails** → the request fails. Nothing is half-applied.
* **rebase conflicts** → the commit stands and the request reports a conflict. The edit is safe in
  local history; a human resolves the divergence.
* **push fails** (offline, auth) → the request *succeeds* with ``pushed: false``. The change is
  committed and cannot be lost, so a network blip must not be reported as a failed edit.

The pull before pushing is not optional: the same config repo is edited from several machines, and
without it the first divergence turns every later write into a rejected non-fast-forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quill.git_ops import GitError, Runner, SubprocessRunner


class CatalogConflict(RuntimeError):
    """The commit landed locally but could not be rebased onto the remote."""


@dataclass(frozen=True, slots=True)
class CommitResult:
    """What happened to one catalog write."""

    committed: bool
    pushed: bool
    sha: str | None = None
    error: str | None = None


class CatalogRepo:
    """The git working tree holding the persona and skill libraries."""

    def __init__(self, root: Path, *, runner: Runner | None = None) -> None:
        # The libraries are usually symlinks into a config repo; resolve so git commands run in the
        # real tree rather than at the symlink, where `git rev-parse` would find another repo.
        self.root = Path(root).resolve()
        self._run: Runner = runner or SubprocessRunner(directory=str(self.root))

    def is_repo(self) -> bool:
        """False when the libraries are plain directories rather than a checkout."""
        try:
            return self._run(["git", "rev-parse", "--is-inside-work-tree"]).strip() == "true"
        except GitError:
            return False

    def commit_and_push(self, paths: list[Path], message: str) -> CommitResult:
        """Stage ``paths``, commit with ``message``, then pull --rebase and push.

        A tree that is not a git repo is not an error: the libraries work perfectly well as plain
        directories, they just have no history to write to.
        """
        if not paths:
            return CommitResult(committed=False, pushed=False, error="nothing to commit")
        if not self.is_repo():
            return CommitResult(
                committed=False, pushed=False, error="catalog root is not a git repository"
            )

        try:
            self._run(["git", "add", "--", *[str(p) for p in paths]])
        except GitError as exc:
            raise RuntimeError(f"could not stage catalog change: {exc}") from exc

        if not self._has_staged_changes():
            # Rewriting a file with identical content is a no-op, not a failure.
            return CommitResult(committed=False, pushed=False, error="no change to commit")

        try:
            self._run(["git", "commit", "-m", message])
        except GitError as exc:
            raise RuntimeError(f"could not commit catalog change: {exc}") from exc

        sha = self._head()

        try:
            self._run(["git", "pull", "--rebase"])
        except GitError as exc:
            # A failed pull means one of two very different things, and telling them apart matters:
            # an actual rebase conflict needs a human, whereas an unreachable remote needs nothing
            # but a retry later. Only the former leaves a rebase in progress.
            if self._rebase_in_progress():
                # Leave it stopped rather than aborting: aborting would hide a real divergence, and
                # the commit is safe either way.
                raise CatalogConflict(
                    f"committed {sha}, but rebasing onto the remote conflicted: {exc}"
                ) from exc
            return CommitResult(committed=True, pushed=False, sha=sha, error=str(exc))

        try:
            self._run(["git", "push"])
        except GitError as exc:
            return CommitResult(committed=True, pushed=False, sha=sha, error=str(exc))

        return CommitResult(committed=True, pushed=True, sha=sha)

    # -- internals ----------------------------------------------------------------

    def _has_staged_changes(self) -> bool:
        try:
            self._run(["git", "diff", "--cached", "--quiet"])
        except GitError:
            return True  # non-zero exit from --quiet means there IS a difference
        return False

    def _rebase_in_progress(self) -> bool:
        """True when git stopped mid-rebase — i.e. the pull hit a real conflict."""
        try:
            git_dir = Path(self._run(["git", "rev-parse", "--git-dir"]).strip())
        except GitError:
            return False
        if not git_dir.is_absolute():
            git_dir = self.root / git_dir
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def _head(self) -> str | None:
        try:
            return self._run(["git", "rev-parse", "--short", "HEAD"]).strip() or None
        except GitError:
            return None


def commit_message(scope: str, name: str, verb: str, target: str, reason: str) -> str:
    """``skills(cpp-pro): update SKILL.md — add move-semantics section``.

    Matches the loose conventional style already in the config repo's history.
    """
    return f"{scope}({name}): {verb} {target} — {reason.strip()}"
