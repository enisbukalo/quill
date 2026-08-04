"""opencode runner — ``opencode run --agent <a> --model llamacpp/<preset> ...`` (WI-16).

The original built-in. Invokes opencode headless with JSON output and skipped permissions,
captures stdout, and enforces the per-spawn timeout via subprocess (opencode has no native
timeout). Its receipt is the last ``type == "text"`` part of the stream — reused from
:func:`quill.phases.extract_receipt`, opencode's canonical shape.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from quill.live_usage import LiveUsage
from quill.phases import SpawnError, extract_receipt
from quill.preflight import PreflightError
from quill.runners import Runner, register_runner
from quill.spawn_io import run_streaming

# Hermetic spawn: we inherit NOTHING from the user's opencode config. Ambient defaults
# (e.g. a global ``default_agent: "plan"`` in ~/.config/opencode/opencode.json) silently
# change behavior per machine — the plan agent writes to ``.opencode/plans/`` instead of the
# run dir the engine injects, and the plan-review phase then finds no plan. We pin the agent to
# the built-in ``build`` (always present, primary) and inject an inline config via
# OPENCODE_CONFIG_CONTENT (highest precedence) so the user's file can't override us.
_BUILD_AGENT = "build"
# Inline config (OPENCODE_CONFIG_CONTENT) — highest-precedence config source. It MERGES over the
# user's global/project config (it does not replace it), so two things are pinned here:
#
#   default_agent — force the built-in ``build`` agent; a bare invocation can't fall back to the
#   user's default_agent (often ``plan``, which writes to ``.opencode/plans/`` and breaks us).
#
#   mcp{}/tools{} — DISABLE the tools that derail a small local model. Left on, the build agent
#   sees the user's MCP servers (engram memory, code-index) and the ``task`` subagent spawner, and
#   a weak model burns its finite step budget on ``engram_mem_search`` / ``task`` rabbit holes and
#   runs out of turn before it writes its artifact + receipt (observed: plan phase ends mid-
#   exploration, no plan.md, no receipt). A quill phase only needs file + shell tools: read, write,
#   edit, glob, grep, bash. We turn the rest off so the model's steps go to the deliverable.
_DISABLED_MCP = ("engram", "code-index", "large-file-mcp")
_DISABLED_TOOLS = ("task", "todowrite", "todoread")
_INLINE_CONFIG = json.dumps(
    {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": _BUILD_AGENT,
        "mcp": {name: {"enabled": False} for name in _DISABLED_MCP},
        "tools": {name: False for name in _DISABLED_TOOLS},
    }
)


@register_runner
@dataclass(slots=True)
class OpencodeRunner(Runner):
    """Drive a phase through the ``opencode`` CLI."""

    name = "opencode"

    directory: str
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    @override
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
        # Resolve the real executable (on Windows opencode is a .cmd/.ps1 shim;
        # subprocess without shell=True can't launch a bare "opencode" -> WinError 2).
        executable = shutil.which("opencode")
        if executable is None:
            raise SpawnError("could not launch opencode: not found on PATH")
        # Hermetic: pin the built-in ``build`` agent rather than relying on opencode's
        # ``default_agent`` (which on the user's machine is ``plan`` and silently breaks the
        # plan-output contract). ``agent`` from the Runner interface is intentionally ignored —
        # quill's persona (in the prompt) carries the role; we only need a deterministic,
        # always-present primary agent here.
        #
        # NOT ``--pure``: it strips the plugin that emits the final ``type:"text"`` part in the
        # JSON stream, so the receipt comes back empty ("no receipt in worker output") even
        # though the model replied. The inline-config override below is enough to neutralize the
        # user's config without dropping plugins.
        _ = agent
        # The prompt is large (persona + ticket) and is passed on STDIN, not as
        # an argv element: Windows caps a command line at ~8191 chars, which a fat prompt blows
        # ("The command line is too long."). opencode `run` reads the prompt from stdin when no
        # positional message is given.
        cmd = [
            executable,
            "run",
            "--agent",
            _BUILD_AGENT,
            "--model",
            f"llamacpp/{preset}",
            "--format",
            "json",
            "--dangerously-skip-permissions",
        ]
        # OPENCODE_CONFIG_CONTENT is the highest-precedence config source: it overrides the
        # user's global/project opencode.json so an ambient default_agent can't leak in even if
        # the flag handling changes. We layer it on top of the inherited env, not replace it.
        env = {**os.environ, "OPENCODE_CONFIG_CONTENT": _INLINE_CONFIG}
        # Force UTF-8 on stdin/stdout: the prompt carries Unicode (arrows, em dashes) from the
        # persona/ticket, and Windows' default cp1252 can't encode them. run_streaming tees the live JSONL
        # stream to stream_path and returns the full stdout for receipt extraction.
        return run_streaming(
            cmd,
            cwd=self.directory,
            stream_path=stream_path,
            agent=f"opencode:{agent}",
            timeout=timeout,
            input_text=prompt,
            env=env,
            on_tool=on_tool,
            on_usage=on_usage,
            should_stop=lambda: "stopped by request" if self._stop.is_set() else None,
            abort_reason=abort_reason,
        )

    @override
    def cancel(self) -> None:
        self._stop.set()

    @override
    def extract_receipt(self, stdout: str) -> str | None:
        return extract_receipt(stdout)

    @override
    def skill_directive(self, names: list[str]) -> str:
        """opencode loads a skill with ``/<name>``. Return one trigger per skill, or ``""``."""
        if not names:
            return ""
        triggers = " ".join(f"/{name}" for name in names)
        return f"First load these skills: {triggers}. Then carry out the task below."

    @override
    def preflight(self) -> None:
        if shutil.which("opencode") is None:
            raise PreflightError(
                "opencode was not found on PATH. Install it and ensure `opencode` runs, "
                "then re-run."
            )
