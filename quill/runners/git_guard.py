"""Phase-aware Git mutation guard for model-driven worker processes."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

_DELIVERY_PHASES = frozenset({"commit", "commit_update"})
_MUTATING_GIT_COMMANDS = frozenset(
    {
        "add",
        "am",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "mv",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
        "tag",
        "update-ref",
    }
)


@contextmanager
def agent_environment(agent: str, inherited: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """Return an environment that denies Git mutations outside delivery phases.

    The model still gets ordinary read-only Git commands such as ``status``, ``diff``, and
    ``log``. Quill's own checkpoint machinery runs outside this child environment, while the
    dedicated commit phases retain the real Git executable and credentials.
    """
    environment = dict(inherited)
    if agent in _DELIVERY_PHASES:
        yield environment
        return

    real_git = shutil.which("git")
    if real_git is None:
        yield environment
        return

    with tempfile.TemporaryDirectory(prefix="quill-agent-bin-") as directory:
        wrapper = Path(directory) / "git"
        wrapper.write_text(_git_wrapper(real_git), encoding="utf-8")
        wrapper.chmod(0o700)
        environment["PATH"] = f"{directory}{os.pathsep}{environment.get('PATH', '')}"
        environment["QUILL_GIT_MUTATIONS"] = "denied"
        yield environment


def _git_wrapper(real_git: str) -> str:
    blocked = repr(sorted(_MUTATING_GIT_COMMANDS))
    return f"""#!/usr/bin/env python3
import os
import sys

blocked = set({blocked})
command = next((arg for arg in sys.argv[1:] if arg in blocked), "")
if command:
    print(
        f"quill: git {{command}} is denied in this phase; only commit/commit_update may mutate Git",
        file=sys.stderr,
    )
    raise SystemExit(77)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
"""
