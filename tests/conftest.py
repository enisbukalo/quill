"""Shared test fixtures for the quill test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill_api.routers import runs as runs_router
from quill_api.routers import project_queue as project_queue_router
from quill_api.routers import system as system_router


@pytest.fixture(autouse=True)
def _gh_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend gh is installed + authenticated so /runs and /health are deterministic.

    Patched per importing module rather than at the source: each router imported the functions by
    name, so rebinding `quill.preflight` would not affect the references they already hold.
    """
    for module in (runs_router, project_queue_router, system_router):
        monkeypatch.setattr(module, "gh_available", lambda: True)
        monkeypatch.setattr(module, "gh_authenticated", lambda: True)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``~`` at a per-test directory.

    The persona library and the runs root default to ``~/.quill/...``
    (:func:`quill.config.default_state_dir`), which is real state on a developer's machine. Without
    this, a test that loads a config would read whatever personas happen to be installed — passing
    or failing depending on the machine — and a test that runs a pipeline would write run artifacts
    into the developer's home. Redirecting ``$HOME`` makes both hermetic.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # vLLM's endpoint is required machine state. Use a reserved example endpoint so tests never
    # depend on or expose a developer's real host configuration.
    monkeypatch.setenv("QUILL_VLLM_URL", "http://vllm.example:8000")
    return home
