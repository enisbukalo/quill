"""Pluggable CLI runners — the harness quill drives each phase through (WI-16).

quill spawns one worker per phase by shelling out to a coding-agent CLI (``opencode``,
``pi``, ...). Every such CLI differs in exactly two places:

* **how it's invoked** — the argv and headless/JSON flags;
* **how its result is read** — the JSON shape its stream emits, from which quill pulls the
  one-line *receipt* it classifies (``DONE:`` / ``PASS:`` / ``BLOCK:`` / ...).

Everything downstream of the receipt — classification, the gate/retry loop, the whole
pipeline — is runner-agnostic and lives in :mod:`quill.phases`. So a runner is fully
described by this small interface, and adding a new CLI is one subclass + one registration,
with no edits to the pipeline.

A runner also owns its **preflight** (is its binary installed?), so a missing CLI fails fast
with an actionable message before any phase starts — and that check ships with the runner
instead of accreting in :mod:`quill.preflight`.

Usage::

    runner = get_runner("pi", directory=repo)   # name comes from quillvault/quillfolio.toml [runner]
    runner.preflight()                           # raises PreflightError if pi isn't on PATH
    stdout = runner.spawn(agent, preset, prompt, timeout=...)
    receipt = runner.extract_receipt(stdout)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from quill.live_usage import LiveUsage


class Runner(ABC):
    """One coding-agent CLI quill can drive a phase through.

    Subclasses implement the two CLI-specific seams (:meth:`spawn`, :meth:`extract_receipt`)
    plus their :meth:`preflight`. Register each with :func:`register_runner` so config can
    select it by :attr:`name`.
    """

    #: Stable identifier used in ``quillvault/quillfolio.toml`` ``[runner] kind`` and the registry.
    name: ClassVar[str]

    #: Target repo the CLI runs in. Subclasses are dataclasses that accept this; declared here
    #: so :func:`get_runner` can construct any runner uniformly.
    directory: str

    #: Whether this runner can re-enter the exact conversation used by its latest spawn.
    supports_session_repair: ClassVar[bool] = False

    def __init__(self, *, directory: str) -> None:
        self.directory = directory

    @abstractmethod
    def spawn(
        self,
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: Callable[[str], None] | None = None,
        on_usage: Callable[[LiveUsage], None] | None = None,
        abort_reason: Callable[[], str | None] | None = None,
    ) -> str:
        """Run one headless worker and return its raw stdout.

        The worker's live JSONL stream is tee'd to ``stream_path`` (flushed per line) as it
        arrives, so the run dir holds a live transcript of every spawn.

        ``on_tool`` is called with each tool's name as the worker starts it, for live progress
        display. A runner whose CLI emits no tool events simply never calls it.

        ``on_usage`` receives monotonic exact provider usage when the runner supports it.

        ``abort_reason`` returns a message when this individual spawn has exceeded a caller-owned
        budget. It must not affect concurrent or later spawns.

        Must raise :class:`quill.phases.SpawnTimeout` on hang and
        :class:`quill.phases.SpawnError` on a non-zero / abnormal exit, so
        :func:`quill.phases.run_phase` can classify those as CRASH.
        """

    @abstractmethod
    def extract_receipt(self, stdout: str) -> str | None:
        """Pull the one-line receipt from this CLI's stdout, or ``None`` if absent.

        Tolerant of CRLF and of non-JSON log noise interleaved in the stream.
        """

    @abstractmethod
    def preflight(self) -> None:
        """Raise :class:`quill.preflight.PreflightError` unless this CLI is ready to run.

        Detection only (binary on PATH); never auto-installs.
        """

    def skill_directive(self, names: list[str]) -> str:
        """A prompt line telling *this* CLI to load the named skills, in its own invocation syntax.

        quill does not store or ship skill bodies — skills live in the coding-agent CLI, set up by
        the user. A phase's ``skills = [...]`` config is just names; the engine appends this line to
        the spawn prompt so the harness loads them. Each CLI has its own trigger syntax (pi:
        ``/skill:<name>``, opencode: ``/<name>``), so each runner formats its own. Empty ``names``
        (or a runner with no skill mechanism) returns ``""`` — nothing is appended.
        """
        return ""

    def available_session_capacity(self, model: str) -> int:
        """Return how many independent sessions may use ``model`` concurrently.

        Capacity discovery is optional. Runners that do not have authoritative model metadata
        remain sequential, which is the safe default for an exclusive local model server.
        """
        _ = model
        return 1

    def cancel(self) -> None:
        """Request cancellation of the currently executing worker, if any."""

    def repair_session(
        self,
        agent: str,
        preset: str,
        prompt: str,
        *,
        timeout: float,
        stream_path: Path,
        on_tool: Callable[[str], None] | None = None,
        on_usage: Callable[[LiveUsage], None] | None = None,
        abort_reason: Callable[[], str | None] | None = None,
    ) -> str:
        """Continue the latest matching session with a corrective prompt.

        Only runners setting :attr:`supports_session_repair` implement this seam. Keeping the
        default explicit makes accidental use fail immediately instead of silently starting a new
        conversation and spending the original phase's context again.
        """
        raise NotImplementedError(f"{self.name} does not support same-session repair")


# -- registry ---------------------------------------------------------------------

_RUNNERS: dict[str, type[Runner]] = {}


def register_runner(cls: type[Runner]) -> type[Runner]:
    """Class decorator: register ``cls`` under its :attr:`Runner.name` for :func:`get_runner`."""
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must set a non-empty class-level `name`")
    _RUNNERS[cls.name] = cls
    return cls


def available_runners() -> tuple[str, ...]:
    """Sorted names of every registered runner (for error messages / introspection)."""
    return tuple(sorted(_RUNNERS))


class UnknownRunnerError(ValueError):
    """``quillvault/quillfolio.toml`` named a ``[runner] kind`` with no registered implementation."""


def get_runner(name: str, *, directory: str) -> Runner:
    """Construct the registered runner called ``name``, bound to ``directory``.

    Raises:
        UnknownRunnerError: ``name`` isn't a registered runner — the message lists the ones
            that are, so a typo in config is obvious.
    """
    key = name.strip().lower()
    cls = _RUNNERS.get(key)
    if cls is None:
        known = ", ".join(available_runners()) or "(none registered)"
        raise UnknownRunnerError(f"unknown runner {name!r}; available runners: {known}")
    return cls(directory=directory)


# Import the built-in runners so importing this package registers them. Kept at the bottom to
# avoid a circular import (each runner module imports Runner/register_runner from here).
from quill.runners import opencode as _opencode  # noqa: F401
from quill.runners import pi as _pi  # noqa: F401
