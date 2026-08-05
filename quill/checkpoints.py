"""Local-only Git checkpoints used to restart a failed run from a phase boundary."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from quill.git_ops import GitError, Runner, SubprocessRunner

MANIFEST_NAME = "phase-checkpoints.json"


@dataclass(frozen=True, slots=True)
class PhaseCheckpoint:
    phase: str
    commit: str
    created_at: float | None = None


@dataclass(frozen=True, slots=True)
class CheckpointManifest:
    run_id: str
    repo: str
    branch: str
    base: str
    phases: tuple[str, ...]
    checkpoints: tuple[PhaseCheckpoint, ...]

    def commit_for(self, phase: str) -> str | None:
        matches = [item.commit for item in self.checkpoints if item.phase == phase]
        return matches[-1] if matches else None


def load_manifest(run_dir: Path) -> CheckpointManifest | None:
    """Read a checkpoint manifest, returning ``None`` for absent or malformed state."""
    try:
        raw = json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        checkpoints = tuple(
            PhaseCheckpoint(
                phase=str(item["phase"]),
                commit=str(item["commit"]),
                created_at=(
                    float(item["created_at"])
                    if isinstance(item.get("created_at"), (int, float))
                    and not isinstance(item.get("created_at"), bool)
                    else None
                ),
            )
            for item in raw["checkpoints"]
        )
        phases = tuple(str(item) for item in raw["phases"])
        return CheckpointManifest(
            run_id=str(raw["run_id"]),
            repo=str(raw["repo"]),
            branch=str(raw["branch"]),
            base=str(raw["base"]),
            phases=phases,
            checkpoints=checkpoints,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


class CheckpointRecorder:
    """Commit the worktree before each phase and retain commits under a private Git ref."""

    def __init__(
        self,
        directory: Path,
        run_dir: Path,
        *,
        run_id: str,
        repo: str,
        branch: str,
        base_branch: str,
        phases: tuple[str, ...],
        runner: Runner | None = None,
    ) -> None:
        self.directory = directory
        self.run_dir = run_dir
        self.run_id = run_id
        self.repo = repo
        self.branch = branch
        self.base_branch = base_branch
        self.phases = phases
        self.run = runner or SubprocessRunner(str(directory))
        self.base = ""
        self.checkpoints: list[PhaseCheckpoint] = []
        self.delivery_started = False
        self._lock = RLock()

    @property
    def ref(self) -> str:
        safe_id = "".join(char if char.isalnum() or char in "._-" else "-" for char in self.run_id)
        return f"refs/quill/runs/{safe_id}"

    def before_phase(self, phase: str) -> str | None:
        """Save the exact state immediately before ``phase``.

        Delivery phases consume the accumulated work as one normal commit, so local checkpoint
        commits are retained by a private ref and removed from the branch first.
        """
        # Parallel producer retries can reach this hook from worker threads. Git index operations
        # and the manifest update remain one serial transaction.
        with self._lock:
            if self.delivery_started:
                return None
            self._ensure_base()
            self._commit(f"quill checkpoint {self.run_id} before {phase}")
            commit = self.run(["git", "rev-parse", "HEAD"]).strip()
            self.run(["git", "update-ref", self.ref, commit])
            self.checkpoints.append(
                PhaseCheckpoint(phase=phase, commit=commit, created_at=time.time())
            )
            self._write()
            if phase in {"commit", "commit_update"}:
                self.run(["git", "reset", "--mixed", self.base])
                self.delivery_started = True
            return commit

    def recover_terminal(self, phase: str | None) -> bool:
        """Capture work left by a failed/halted phase. Return whether useful changes exist."""
        self._ensure_base()
        if not self.run(["git", "status", "--porcelain"]).strip() and not self._has_diff():
            return False
        label = phase or "terminal"
        self._commit(f"quill recovery {self.run_id} after {label}")
        commit = self.run(["git", "rev-parse", "HEAD"]).strip()
        self.run(["git", "update-ref", self.ref, commit])
        self._write()
        return self._has_diff()

    def _ensure_base(self) -> None:
        if not self.base:
            self.base = self.run(["git", "rev-parse", f"origin/{self.base_branch}"]).strip()

    def _has_diff(self) -> bool:
        return bool(self.run(["git", "diff", "--name-only", f"{self.base}..HEAD"]).strip())

    def _commit(self, message: str) -> None:
        self.run(["git", "add", "-A"])
        self.run(
            [
                "git",
                "-c",
                "user.name=Quill",
                "-c",
                "user.email=quill@localhost",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ]
        )

    def _write(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "run_id": self.run_id,
            "repo": self.repo,
            "branch": self.branch,
            "base": self.base,
            "phases": list(self.phases),
            "checkpoints": [
                {
                    "phase": item.phase,
                    "commit": item.commit,
                    "created_at": item.created_at,
                }
                for item in self.checkpoints
            ],
        }
        target = self.run_dir / MANIFEST_NAME
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)


def restore_checkpoint(directory: Path, branch: str, commit: str) -> None:
    """Reset an existing local branch to a recorded checkpoint and clean untracked files."""
    run = SubprocessRunner(str(directory))
    try:
        run(["git", "cat-file", "-e", f"{commit}^{{commit}}"])
        run(["git", "checkout", branch])
        run(["git", "reset", "--hard", commit])
        run(["git", "clean", "-fd"])
    except GitError as exc:
        raise GitError(f"could not restore checkpoint {commit[:12]}: {exc}") from exc
