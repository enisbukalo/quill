"""Deterministic Git commit attribution for Quill's commit phase."""

from __future__ import annotations

import shlex
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def commit_attribution(directory: Path, run_dir: Path, model: str) -> Iterator[None]:
    """Append Quill/model trailers to commits made while this context is active.

    The commit phase owns the actual ``git commit`` invocation, so a temporary ``commit-msg``
    hook provides attribution without trusting the model to compose it. Any existing repository
    hook is chained and restored byte-for-byte afterwards.
    """
    resolved = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks/commit-msg"],
        cwd=directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if resolved.returncode != 0:
        # Test/injected contexts may deliberately have no checkout; real runs pass preflight first.
        yield
        return
    git_path = resolved.stdout.strip()
    hook = Path(git_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    previous = hook.read_bytes() if hook.exists() else None
    previous_mode = stat.S_IMODE(hook.stat().st_mode) if hook.exists() else None
    chained = run_dir / "commit-msg.previous"
    if previous is not None:
        chained.write_bytes(previous)
        chained.chmod(previous_mode or 0o755)

    chained_command = (
        f'if [ -x {shlex.quote(str(chained))} ]; then {shlex.quote(str(chained))} "$@"; fi\n'
        if previous is not None
        else ""
    )
    script = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"{chained_command}"
        "git interpret-trailers --if-exists doNothing --if-missing add "
        "--trailer 'Generated-by: Quill' "
        f'--trailer {shlex.quote(f"Model: {model}")} --in-place "$1"\n'
    )
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)
    try:
        yield
    finally:
        if previous is None:
            hook.unlink(missing_ok=True)
        else:
            hook.write_bytes(previous)
            hook.chmod(previous_mode or 0o755)
        chained.unlink(missing_ok=True)
