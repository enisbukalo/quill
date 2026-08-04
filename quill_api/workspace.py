"""Per-repo checkouts the service runs pipelines in (server milestone B1).

A run arrives as ``{repo, branch, ticket}`` — no filesystem, because the client may be on
a different machine. This module turns that into a real checkout on the server: clone the repo if
it is new, otherwise fetch and reset it to the requested branch.

**One persistent clone per repo, not one per run.** Runs are serialised (the GPU is exclusive), so
concurrent worktrees would buy nothing, and a persistent checkout keeps build caches — which for a
C++/CMake repo is the difference between an incremental rebuild and a cold one every run. That is
also why the clean is ``git clean -fd`` and not ``-fdx``: ignored files (``build/``, ``.venv/``)
survive deliberately.

Every value that reaches a path join or a subprocess is validated first. ``repo`` and ``branch``
arrive over HTTP, so ``../..`` or a shell metacharacter must be refused before it becomes a
directory or an argv entry.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quill.git_ops import GitError, Runner, SubprocessRunner

#: Builds a command runner bound to a directory. Injected so tests drive the git sequence without
#: real subprocesses.
type RunnerFactory = Callable[[str], Runner]

#: ``owner/name``. Deliberately narrow: GitHub allows only these characters, and anything else is
#: either a typo or an attempt to escape the workspace root.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: A git branch name, minus the things ``git check-ref-format`` forbids. Leading dashes are
#: excluded so a branch can never be read as a command-line flag.
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/-]+$")


class WorkspaceError(RuntimeError):
    """A workspace could not be prepared. The message is user-facing (surfaced as a 4xx/5xx).

    The subclasses below let the HTTP layer map a failure to the right status code without parsing
    the message: a bad ref is a 422, a missing checkout a 404, a dirty/diverged worktree a 409, and
    an unexpected git failure a 502. A bare :class:`WorkspaceError` (raised by :func:`validate_repo`
    / :func:`validate_branch`) stays a 422 — malformed input the client can fix.
    """


class WorkspaceNotFound(WorkspaceError):
    """The requested checkout, branch, or remote ref does not exist (HTTP 404)."""


class WorkspaceConflict(WorkspaceError):
    """The operation would race real state: a dirty or diverged worktree, or a protected/default
    branch (HTTP 409). Nothing was changed."""


class WorkspaceGitError(WorkspaceError):
    """A git command failed for a reason the operator cannot pre-empt (HTTP 502)."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """A prepared checkout: which repo and branch, and where it landed on disk."""

    repo: str
    branch: str
    path: Path


@dataclass(frozen=True, slots=True)
class ConfigWorkspace:
    """Checkout containing the config used to finish preparing a requested branch."""

    workspace: Workspace
    requested_branch_exists: bool


@dataclass(frozen=True, slots=True)
class WorkspaceBranch:
    """One selectable branch of a checkout, and where it exists.

    A branch can be ``local`` only (created here, never pushed), ``remote`` only (on origin but not
    yet checked out), or both. ``current`` marks the branch HEAD is on. The server path is never
    part of this record — it is server-internal and must not leak through the API.
    """

    name: str
    current: bool
    local: bool
    remote: bool


@dataclass(frozen=True, slots=True)
class WorkspaceMutation:
    """The result of a branch operation: which branch the checkout now sits on, and a message
    describing what changed, both safe to show an operator."""

    repo: str
    branch: str
    message: str


@dataclass(frozen=True, slots=True)
class RestartBranchStatus:
    """Git facts that determine whether a local run branch can be resumed safely."""

    eligible: bool
    reason: str | None = None
    ahead: int = 0
    behind: int = 0


def validate_repo(repo: str) -> str:
    """Return ``repo`` if it is a well-formed ``owner/name``, else raise."""
    candidate = repo.strip()
    if not _REPO_RE.match(candidate):
        raise WorkspaceError(
            f"invalid repo {repo!r} — expected 'owner/name' using letters, digits, '.', '_', '-'."
        )
    return candidate


def validate_branch(branch: str) -> str:
    """Return ``branch`` if git would accept it as a ref name, else raise."""
    candidate = branch.strip()
    if not candidate or len(candidate) > 255 or not _BRANCH_RE.match(candidate):
        raise WorkspaceError(
            f"invalid branch {branch!r} — must be a valid git ref name and cannot start with '-'."
        )
    if candidate.endswith((".lock", "/", ".")):
        raise WorkspaceError(f"invalid branch {branch!r} — git rejects this ref name.")
    return candidate


class WorkspaceManager:
    """Owns ``<root>/<owner>/<name>`` checkouts and readies one for each run."""

    def __init__(
        self,
        root: Path,
        *,
        runner_factory: RunnerFactory | None = None,
        git_author: tuple[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self._runner_factory: RunnerFactory = runner_factory or (
            lambda directory: SubprocessRunner(directory)
        )
        #: ``(name, email)`` stamped repo-locally on every checkout. ``None`` leaves each
        #: checkout inheriting the service user's global git identity — never the case in a
        #: real deployment, where :class:`Settings` always supplies one.
        self._git_author = git_author
        # One lock per repo, created on demand. Two server threads must never mutate the same
        # checkout at once (a prepare racing a manual pull would corrupt HEAD), but different repos
        # are independent, so a single global lock would needlessly serialise them.
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, repo: str) -> threading.RLock:
        """The lock guarding ``repo``'s checkout, shared across every operation on it."""
        key = validate_repo(repo)
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    def path_for(self, repo: str) -> Path:
        """Where ``repo``'s checkout lives. Validated, so it cannot escape :attr:`root`."""
        owner, name = validate_repo(repo).split("/", 1)
        return self.root / owner / name

    def checkouts(self) -> list[Workspace]:
        """Every checkout currently on disk, with the branch each is sitting on."""
        found: list[Workspace] = []
        if not self.root.is_dir():
            return found
        for owner in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for repo_dir in sorted(p for p in owner.iterdir() if (p / ".git").exists()):
                repo = f"{owner.name}/{repo_dir.name}"
                found.append(
                    Workspace(repo=repo, branch=self._current_branch(repo_dir), path=repo_dir)
                )
        return found

    def local_branches(self, repo: str) -> set[str]:
        """Return local branch refs without fetching or otherwise mutating the checkout."""
        repo = validate_repo(repo)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            try:
                return self._ref_names(self._run_in(path), "refs/heads")
            except GitError as exc:
                raise WorkspaceGitError(f"could not list local branches for {repo}: {exc}") from exc

    def restart_status(
        self,
        repo: str,
        branch: str,
        *,
        base: str,
        checkpoint_base: str | None = None,
    ) -> RestartBranchStatus:
        """Return restart eligibility, allowing non-overlapping advances on the base branch."""
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        base = validate_branch(base)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            run = self._run_in(path)
            try:
                run(["git", "fetch", "origin", "--prune"])
                if not self._local_has(run, branch):
                    return RestartBranchStatus(False, f"local branch '{branch}' no longer exists")
                if not self._remote_has(run, base):
                    return RestartBranchStatus(False, f"origin/{base} does not exist")
                counts = run(
                    ["git", "rev-list", "--left-right", "--count", f"origin/{base}...{branch}"]
                ).split()
                behind, ahead = (int(counts[0]), int(counts[1]))
                if behind:
                    if checkpoint_base is None:
                        return RestartBranchStatus(
                            False,
                            f"branch is {behind} commit(s) behind origin/{base}",
                            ahead,
                            behind,
                        )
                    merge_base = run(
                        ["git", "merge-base", checkpoint_base, f"origin/{base}"]
                    ).strip()
                    if merge_base != checkpoint_base:
                        return RestartBranchStatus(
                            False,
                            f"origin/{base} no longer descends from the run's recorded base",
                            ahead,
                            behind,
                        )
                    run_files = self._changed_files(run, f"origin/{base}...{branch}")
                    upstream_files = self._changed_files(run, f"{checkpoint_base}..origin/{base}")
                    overlap = sorted(run_files & upstream_files)
                    if overlap:
                        shown = ", ".join(overlap[:5])
                        if len(overlap) > 5:
                            shown += f", and {len(overlap) - 5} more"
                        return RestartBranchStatus(
                            False,
                            f"origin/{base} advanced across file(s) changed by the run: {shown}",
                            ahead,
                            behind,
                        )
                if ahead < 1:
                    return RestartBranchStatus(False, f"branch is not ahead of origin/{base}")
                changed = self._changed_files(run, f"origin/{base}...{branch}")
                if not changed:
                    return RestartBranchStatus(False, "branch has no changes to recover", ahead, 0)
                raw_prs = run(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        repo,
                        "--head",
                        branch,
                        "--state",
                        "all",
                        "--limit",
                        "1",
                        "--json",
                        "number,state",
                    ]
                )
                prs = json.loads(raw_prs or "[]")
                if prs:
                    return RestartBranchStatus(False, "branch is already associated with a PR")
            except (GitError, ValueError, json.JSONDecodeError) as exc:
                raise WorkspaceGitError(
                    f"could not inspect restart branch '{branch}': {exc}"
                ) from exc
        return RestartBranchStatus(True, ahead=ahead, behind=behind)

    def branch_has_pull_request(self, repo: str, branch: str) -> bool:
        """Whether GitHub has any open or closed PR whose head is this branch."""
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            try:
                raw = self._run_in(path)(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        repo,
                        "--head",
                        branch,
                        "--state",
                        "all",
                        "--limit",
                        "1",
                        "--json",
                        "number",
                    ]
                )
                return bool(json.loads(raw or "[]"))
            except (GitError, json.JSONDecodeError) as exc:
                raise WorkspaceGitError(
                    f"could not inspect pull requests for '{branch}': {exc}"
                ) from exc

    def restore_run_checkpoint(
        self, repo: str, branch: str, commit: str, *, base: str | None = None
    ) -> Workspace:
        """Restore a retained checkpoint and reconcile a safely advanced base branch."""
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        base = validate_branch(base) if base is not None else None
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            run = self._run_in(path)
            original = ""
            try:
                if not self._local_has(run, branch):
                    raise WorkspaceNotFound(f"{repo} has no local branch '{branch}'.")
                run(["git", "cat-file", "-e", f"{commit}^{{commit}}"])
                original = run(["git", "rev-parse", branch]).strip()
                run(["git", "checkout", branch])
                run(["git", "reset", "--hard", commit])
                run(["git", "clean", "-fd"])
                if base is not None:
                    run(["git", "merge", "--no-edit", f"origin/{base}"])
            except GitError as exc:
                if original:
                    try:
                        run(["git", "merge", "--abort"])
                    except GitError:
                        pass
                    try:
                        run(["git", "reset", "--hard", original])
                        run(["git", "clean", "-fd"])
                    except GitError:
                        pass
                raise WorkspaceGitError(
                    f"could not restore checkpoint {commit[:12]} on '{branch}': {exc}"
                ) from exc
        return Workspace(repo=repo, branch=branch, path=path)

    @staticmethod
    def _changed_files(run: Runner, revision: str) -> set[str]:
        """Return every path touched by ``revision``, treating renames as delete-plus-add."""
        output = run(["git", "diff", "--name-only", "--no-renames", revision, "--"])
        return {line.strip() for line in output.splitlines() if line.strip()}

    def delete_run_checkpoint_ref(self, repo: str, run_id: str) -> None:
        """Delete only Quill's private retention ref for a deleted run, when present."""
        repo = validate_repo(repo)
        path = self.path_for(repo)
        if not (path / ".git").exists():
            return
        safe_id = "".join(char if char.isalnum() or char in "._-" else "-" for char in run_id)
        with self._lock_for(repo):
            run = self._run_in(path)
            try:
                run(["git", "show-ref", "--verify", "--quiet", f"refs/quill/runs/{safe_id}"])
            except GitError:
                return
            try:
                run(["git", "update-ref", "-d", f"refs/quill/runs/{safe_id}"])
            except GitError as exc:
                raise WorkspaceGitError(
                    f"could not delete checkpoint ref for run {run_id}: {exc}"
                ) from exc

    def prepare(self, repo: str, branch: str, *, base: str) -> Workspace:
        """Ready a clean checkout of ``repo`` on ``branch``; return where it is.

        Clones on first use. Then fetches, checks out ``branch`` (creating it from ``base`` when it
        does not exist on the remote yet), hard-resets, and removes untracked files.

        Raises:
            WorkspaceError: the repo/branch is malformed, or a git/gh command failed.
        """
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        base = validate_branch(base)
        path = self.path_for(repo)

        with self._lock_for(repo):
            self._ensure_checkout(repo, path)
            return self._prepare_locked(repo, branch, base, path)

    def _prepare_locked(self, repo: str, branch: str, base: str, path: Path) -> Workspace:
        run = self._run_in(path)
        try:
            run(["git", "fetch", "origin", "--prune"])
            if self._remote_has(run, branch):
                # Existing branch: take the remote's state verbatim. A local branch left over from
                # an earlier run may be stale or diverged, and the remote is the shared truth.
                run(["git", "checkout", "-B", branch, f"origin/{branch}"])
                run(["git", "reset", "--hard", f"origin/{branch}"])
            else:
                if not self._remote_has(run, base):
                    raise WorkspaceError(
                        f"{repo} has neither branch '{branch}' nor base branch '{base}' on origin."
                    )
                run(["git", "checkout", "-B", branch, f"origin/{base}"])
                run(["git", "reset", "--hard", f"origin/{base}"])
            # `-fd` not `-fdx`: ignored files are build caches worth keeping between runs.
            run(["git", "clean", "-fd"])
        except GitError as exc:
            raise WorkspaceError(f"could not prepare {repo} on '{branch}': {exc}") from exc

        return Workspace(repo=repo, branch=branch, path=path)

    def prepare_for_config(self, repo: str, branch: str) -> ConfigWorkspace:
        """Check out the source from which a run's committed config must be loaded.

        An existing requested branch is authoritative. For a new branch, the remote default branch
        is checked out first so its ``quillfolio.toml`` can name the actual ``repo.pr_base`` used by
        :meth:`prepare` afterward.
        """
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        path = self.path_for(repo)
        with self._lock_for(repo):
            self._ensure_checkout(repo, path)
            return self._prepare_for_config_locked(repo, branch, path)

    def prepare_default_for_config(self, repo: str) -> ConfigWorkspace:
        """Load committed run configuration from the remote default branch.

        Pull-request review workflows are orchestration policy, not reviewed source. Loading them
        from the default branch allows Quill to review PRs opened before the workflow was added.
        The caller must subsequently prepare the exact PR branch for execution.
        """
        repo = validate_repo(repo)
        path = self.path_for(repo)
        with self._lock_for(repo):
            self._ensure_checkout(repo, path)
            run = self._run_in(path)
            try:
                run(["git", "fetch", "origin", "--prune"])
                source = self._default_branch(run, repo)
                run(["git", "checkout", "-B", source, f"origin/{source}"])
                run(["git", "reset", "--hard", f"origin/{source}"])
                run(["git", "clean", "-fd"])
            except GitError as exc:
                raise WorkspaceError(f"could not prepare {repo} to load its config: {exc}") from exc

        return ConfigWorkspace(
            workspace=Workspace(repo=repo, branch=source, path=path),
            requested_branch_exists=False,
        )

    def _prepare_for_config_locked(self, repo: str, branch: str, path: Path) -> ConfigWorkspace:
        run = self._run_in(path)
        try:
            run(["git", "fetch", "origin", "--prune"])
            requested_exists = self._remote_has(run, branch)
            source = branch if requested_exists else self._default_branch(run, repo)
            run(["git", "checkout", "-B", source, f"origin/{source}"])
            run(["git", "reset", "--hard", f"origin/{source}"])
            run(["git", "clean", "-fd"])
        except GitError as exc:
            raise WorkspaceError(f"could not prepare {repo} to load its config: {exc}") from exc

        return ConfigWorkspace(
            workspace=Workspace(repo=repo, branch=source, path=path),
            requested_branch_exists=requested_exists,
        )

    # -- operator branch administration -------------------------------------------

    def branches(self, repo: str) -> list[WorkspaceBranch]:
        """Every branch an operator can select for ``repo``'s checkout.

        Fetches and prunes first so the remote view is fresh, then merges local ``refs/heads`` with
        ``refs/remotes/origin`` so a remote-only branch is offered for checkout and a local-only
        branch is still shown. The symbolic ``origin/HEAD`` is dropped — it is an alias, not a
        branch — and the checked-out branch sorts first.

        Raises:
            WorkspaceNotFound: ``repo`` has no checkout on this server yet.
            WorkspaceGitError: a git command failed.
        """
        repo = validate_repo(repo)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            run = self._run_in(path)
            try:
                run(["git", "fetch", "origin", "--prune"])
                current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
                local = self._ref_names(run, "refs/heads")
                remote = self._ref_names(run, "refs/remotes/origin")
            except GitError as exc:
                raise WorkspaceGitError(f"could not list branches for {repo}: {exc}") from exc

        # `origin/HEAD` is a symbolic alias for the default branch, not a branch of its own.
        remote.discard("HEAD")
        branches = [
            WorkspaceBranch(
                name=validate_branch(name),
                current=(name == current),
                local=(name in local),
                remote=(name in remote),
            )
            for name in local | remote
        ]
        branches.sort(key=lambda item: (not item.current, item.name))
        return branches

    def pull_branch(self, repo: str, branch: str) -> WorkspaceMutation:
        """Fetch ``branch`` and fast-forward the checkout to ``origin/<branch>``.

        Fast-forward only, deliberately: unlike :meth:`prepare`, which hard-resets a checkout to a
        deterministic state for a run, a manual pull must never silently discard an operator's
        local commits or working changes. A dirty or diverged worktree fails visibly instead.

        Raises:
            WorkspaceNotFound: no checkout, or ``origin/<branch>`` does not exist.
            WorkspaceConflict: the worktree is dirty, or the branch cannot fast-forward.
            WorkspaceGitError: a git command failed.
        """
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            run = self._run_in(path)
            self._require_clean(run, repo)
            try:
                run(["git", "fetch", "origin", "--prune"])
            except GitError as exc:
                raise WorkspaceGitError(f"could not fetch origin for {repo}: {exc}") from exc
            if not self._remote_has(run, branch):
                raise WorkspaceNotFound(f"{repo} has no branch '{branch}' on origin.")
            try:
                if self._local_has(run, branch):
                    run(["git", "checkout", branch])
                else:
                    run(["git", "checkout", "-b", branch, "--track", f"origin/{branch}"])
            except GitError as exc:
                raise WorkspaceGitError(f"could not check out '{branch}' in {repo}: {exc}") from exc
            try:
                run(["git", "pull", "--ff-only", "origin", branch])
            except GitError as exc:
                raise WorkspaceConflict(
                    f"{repo} '{branch}' could not fast-forward to origin — it has diverged or "
                    f"carries local commits. Reconcile it manually before pulling."
                ) from exc
        return WorkspaceMutation(
            repo=repo,
            branch=branch,
            message=f"Fast-forwarded '{branch}' to origin/{branch}.",
        )

    def delete_branch(self, repo: str, branch: str) -> WorkspaceMutation:
        """Delete the **local** branch ``branch`` from ``repo``'s checkout. Origin is untouched.

        Deleting the currently checked-out branch first switches the checkout to the remote default
        branch (requiring a clean worktree), because git refuses to delete the branch HEAD is on.
        The remote default branch itself can never be deleted.

        Raises:
            WorkspaceNotFound: no checkout, or no local branch by that name.
            WorkspaceConflict: the branch is the default, or the worktree is dirty.
            WorkspaceGitError: a git command failed.
        """
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        with self._lock_for(repo):
            run = self._run_in(path)
            if not self._local_has(run, branch):
                raise WorkspaceNotFound(f"{repo} has no local branch '{branch}' to delete.")
            try:
                default = self._default_branch(run, repo)
            except WorkspaceError as exc:
                raise WorkspaceGitError(str(exc)) from exc
            if branch == default:
                raise WorkspaceConflict(
                    f"'{branch}' is {repo}'s default branch and cannot be deleted."
                )
            current = self._current_branch(path)
            switched = False
            if branch == current:
                self._require_clean(run, repo)
                try:
                    run(["git", "fetch", "origin", "--prune"])
                    run(["git", "checkout", "-B", default, f"origin/{default}"])
                    run(["git", "reset", "--hard", f"origin/{default}"])
                except GitError as exc:
                    raise WorkspaceGitError(
                        f"could not switch {repo} to '{default}': {exc}"
                    ) from exc
                switched = True
            try:
                run(["git", "branch", "-D", branch])
            except GitError as exc:
                raise WorkspaceGitError(
                    f"could not delete local '{branch}' in {repo}: {exc}"
                ) from exc
        if switched:
            message = (
                f"Deleted local '{branch}'. Checkout switched to default '{default}'. "
                f"origin/{branch} is preserved."
            )
            return WorkspaceMutation(repo=repo, branch=default, message=message)
        return WorkspaceMutation(
            repo=repo,
            branch=current,
            message=f"Deleted local '{branch}'. origin/{branch} is preserved.",
        )

    def discard_run_branch(
        self, repo: str, branch: str, *, base: str = "main"
    ) -> WorkspaceMutation:
        """Destructively clean a failed run checkout and remove its local branch.

        This is intentionally separate from operator deletion: failed/halted run cleanup is
        explicitly allowed to discard tracked and untracked work. The remote branch is untouched.
        """
        repo = validate_repo(repo)
        branch = validate_branch(branch)
        base = validate_branch(base)
        path = self.path_for(repo)
        self._require_checkout(repo, path)
        deleted = False
        with self._lock_for(repo):
            run = self._run_in(path)
            try:
                run(["git", "reset", "--hard"])
                run(["git", "clean", "-fd"])
                run(["git", "fetch", "origin", "--prune"])
                if not self._remote_has(run, base):
                    raise WorkspaceNotFound(f"{repo} has no '{base}' branch on origin.")
                run(["git", "checkout", "-B", base, f"origin/{base}"])
                run(["git", "reset", "--hard", f"origin/{base}"])
                run(["git", "clean", "-fd"])
                if branch != base and self._local_has(run, branch):
                    run(["git", "branch", "-D", branch])
                    deleted = True
            except GitError as exc:
                raise WorkspaceGitError(
                    f"could not clean failed run branch '{branch}' in {repo}: {exc}"
                ) from exc

        action = f" and deleted local '{branch}'" if deleted else ""
        return WorkspaceMutation(
            repo=repo,
            branch=base,
            message=f"Discarded failed run changes{action}; checkout is now '{base}'.",
        )

    # -- internals ----------------------------------------------------------------

    def _require_checkout(self, repo: str, path: Path) -> None:
        if not (path / ".git").exists():
            raise WorkspaceNotFound(f"{repo} has no checkout on this server yet.")

    def _require_clean(self, run: Runner, repo: str) -> None:
        """Refuse to touch a checkout with uncommitted tracked or untracked changes."""
        try:
            status = run(["git", "status", "--porcelain"])
        except GitError as exc:
            raise WorkspaceGitError(f"could not read {repo}'s status: {exc}") from exc
        if status.strip():
            raise WorkspaceConflict(
                f"{repo} has uncommitted changes; commit, stash, or discard them first."
            )

    @staticmethod
    def _ref_names(run: Runner, prefix: str) -> set[str]:
        """Branch short-names under ``prefix`` (``refs/heads`` or ``refs/remotes/origin``)."""
        out = run(["git", "for-each-ref", "--format=%(refname)", prefix])
        names: set[str] = set()
        for line in out.splitlines():
            ref = line.strip()
            if ref.startswith(prefix + "/"):
                names.add(ref.removeprefix(prefix + "/"))
        return names

    @staticmethod
    def _local_has(run: Runner, branch: str) -> bool:
        """True when a local ``refs/heads/<branch>`` exists."""
        try:
            run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
            return True
        except GitError:
            return False

    def _run_in(self, directory: Path) -> Runner:
        return self._runner_factory(str(directory))

    def _clone(self, repo: str, path: Path) -> None:
        """Clone via ``gh``, which reuses the server's existing GitHub auth."""
        path.parent.mkdir(parents=True, exist_ok=True)
        run = self._run_in(path.parent)
        try:
            run(["gh", "repo", "clone", repo, str(path)])
        except GitError as exc:
            raise WorkspaceError(f"could not clone {repo}: {exc}") from exc

    def _ensure_checkout(self, repo: str, path: Path) -> None:
        """Clone ``repo`` if absent, then stamp the configured commit authorship.

        The identity is re-applied on every entry rather than only after a clone: checkouts
        created before the setting existed, or by an earlier deployment with a different
        account, would otherwise keep committing under the service user's global identity
        for the rest of their lives.
        """
        if not (path / ".git").exists():
            self._clone(repo, path)
        if self._git_author is None:
            return
        name, email = self._git_author
        run = self._run_in(path)
        try:
            run(["git", "config", "user.name", name])
            run(["git", "config", "user.email", email])
        except GitError as exc:
            raise WorkspaceError(f"could not set commit identity for {repo}: {exc}") from exc

    @staticmethod
    def _remote_has(run: Runner, branch: str) -> bool:
        """True when ``origin`` publishes ``branch``."""
        try:
            return bool(run(["git", "ls-remote", "--heads", "origin", branch]).strip())
        except GitError:
            return False

    @staticmethod
    def _default_branch(run: Runner, repo: str) -> str:
        try:
            remote_head = run(["git", "ls-remote", "--symref", "origin", "HEAD"])
        except GitError as exc:
            raise WorkspaceError(f"could not determine {repo}'s default branch: {exc}") from exc
        prefix = "ref: refs/heads/"
        for line in remote_head.splitlines():
            if line.startswith(prefix) and line.endswith("\tHEAD"):
                return validate_branch(line.removeprefix(prefix).removesuffix("\tHEAD"))
        raise WorkspaceError(f"could not determine {repo}'s default branch from origin HEAD.")

    def _current_branch(self, path: Path) -> str:
        try:
            return self._run_in(path)(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except GitError:
            return ""
