"""GitHub CLI preflight (WI-13).

The driver shells out to ``gh`` for ticket lookup, PR creation, and the project board. A
missing or unauthenticated ``gh`` should fail **before** a run starts, with a message that
tells the user exactly what to do — not a cryptic subprocess error three phases in.

We only **detect and report**. Installing and authenticating ``gh`` is the user's job; this
module never auto-installs or auto-logs-in. Detection is two cheap subprocess calls
(``gh --version`` / ``gh auth status``) checked by return code — no tokens are parsed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx

_INSTALL_URL = "https://cli.github.com"


class PreflightError(RuntimeError):
    """A required external tool is missing or not ready. The message is user-facing."""


def _run_gh(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``gh <args>``; return the completed process, or None if gh isn't on PATH."""
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None


def gh_available() -> bool:
    """True if the ``gh`` binary is on PATH and runs."""
    proc = _run_gh("--version")
    return proc is not None and proc.returncode == 0


def gh_authenticated() -> bool:
    """True if ``gh`` is authenticated (``gh auth status`` exits 0)."""
    proc = _run_gh("auth", "status")
    return proc is not None and proc.returncode == 0


def gh_version() -> str | None:
    """First line of ``gh --version`` (e.g. ``gh version 2.83.1``), or None if unavailable."""
    proc = _run_gh("--version")
    if proc is None or proc.returncode != 0:
        return None
    first = proc.stdout.splitlines()
    return first[0].strip() if first else None


def check_gh() -> None:
    """Raise :class:`PreflightError` unless ``gh`` is installed *and* authenticated.

    Call this before a run that touches GitHub (which is every non-trivial run — even a
    every run pulls the ticket via ``gh issue view``).
    """
    if not gh_available():
        raise PreflightError(
            f"GitHub CLI (gh) was not found on PATH. Install it: {_INSTALL_URL} "
            "then run `gh auth login`."
        )
    if not gh_authenticated():
        raise PreflightError(
            "GitHub CLI (gh) is installed but not authenticated. Run `gh auth login`."
        )


def check_target_dir(directory: str) -> None:
    """Raise :class:`PreflightError` unless ``directory`` is a git repo with an origin remote.

    Guards the ``quill <ticket>`` default of shipping the **current directory**: running from
    the wrong place (no repo, or a repo with no remote) would otherwise fail confusingly
    mid-phase-0 instead of up front.
    """
    path = Path(directory)
    if not path.is_dir():
        raise PreflightError(f"target directory does not exist: {directory}")

    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise PreflightError(
            f"{directory} is not a git repository. Run quill from inside the target repo, "
            "or pass --dir <repo>."
        )

    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        raise PreflightError(
            f"{directory} has no 'origin' remote — quill needs one to pull the ticket and "
            "open a PR."
        )


def check_opencode() -> None:
    """Raise :class:`PreflightError` unless the ``opencode`` binary is on PATH."""
    if shutil.which("opencode") is None:
        raise PreflightError(
            "opencode was not found on PATH. Install it and ensure `opencode` runs, then re-run."
        )


def check_router(host: str) -> None:
    """Raise :class:`PreflightError` unless the llama.cpp router answers at ``host``.

    Every phase loads a model, so a down router fails the run
    regardless. A clear up-front message beats a CRASH three phases in.
    """
    try:
        resp = httpx.get(f"{host.rstrip('/')}/models", timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise PreflightError(
            f"llama.cpp router is not reachable at {host} ({exc}). Start the router, then re-run."
        ) from exc


def check_vllm(url: str, *, clear_prefix_cache: bool = False) -> None:
    """Raise :class:`PreflightError` unless the vllm server is healthy at ``url``.

    Health is always required. The reset route is probed only for a run that explicitly requested
    cold phase boundaries; it is mounted only under ``VLLM_SERVER_DEV_MODE=1``.
    """
    base = url.rstrip("/")
    try:
        resp = httpx.get(f"{base}/health", timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise PreflightError(
            f"vllm server is not reachable at {url} ({exc}). Start it, then re-run."
        ) from exc
    if not clear_prefix_cache:
        return
    try:
        reset = httpx.post(f"{base}/reset_prefix_cache", timeout=5.0)
    except httpx.HTTPError as exc:
        raise PreflightError(
            f"vllm at {url} did not answer POST /reset_prefix_cache ({exc})."
        ) from exc
    if reset.status_code == 404:
        raise PreflightError(
            f"vllm at {url} has no /reset_prefix_cache route — it was launched without "
            "VLLM_SERVER_DEV_MODE=1. Relaunch the server with that env var set, then re-run."
        )
    if reset.status_code // 100 != 2:
        raise PreflightError(
            f"vllm at {url} answered POST /reset_prefix_cache with {reset.status_code}."
        )
