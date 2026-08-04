"""pi runner — ``pi -p <prompt> --mode json --model <model> ...`` (WI-16).

pi (https://pi.dev / https://github.com/badlogic/pi-mono) is an alternative coding-agent CLI.
Two things differ from opencode and nothing else does:

* **Invocation.** Headless one-shot is ``pi -p`` reading the message from STDIN; JSON output is
  ``--mode json``; approval prompts are skipped with ``-a`` (the user can also set
  ``defaultProjectTrust: "always"``). Like opencode, the persona/role is carried in the STDIN
  prompt, not a CLI flag — the ``agent`` arg is ignored. The model string is opaque to quill — the
  user's pi ``models.json`` resolves it (to the llama.cpp router or the vllm server), exactly as
  the user's ``opencode.json`` does for opencode.

* **Receipt shape.** ``--mode json`` emits an *event stream*, one JSON object per line. The
  final assistant text lives in the **last** ``message_end`` event under ``message.content``,
  whose entries are parts like ``{"type": "text", "text": ...}``. The receipt is the last text
  part of that final message. See :func:`extract_pi_receipt`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from quill.live_usage import LiveUsage
from quill.phases import SpawnError, _normalize_receipt_line
from quill.preflight import PreflightError
from quill.runners import Runner, register_runner
from quill.spawn_io import run_streaming

_VLLM_USAGE_EXTENSION = Path(__file__).parents[1] / "pi_extensions" / "vllm_live_usage.mjs"
_DEFAULT_PI_AGENT_DIR = Path("~/.pi/agent")
_PROC_ROOT = Path("/proc")

# Standing headless contract, injected into pi's SYSTEM prompt (``--append-system-prompt``) so it
# is re-presented on EVERY turn, not just the first user message. pi runs multi-turn under
# ``-p --mode json``: after a turn's tool batch completes, the model re-reads context to decide its
# next turn. A weak model (observed: Qwen3.6_27B_FP8, commit phase) whose turn-1 tools produced only
# read-only output (git status/diff) then sees raw tool output as the most-recent content, forgets
# the task that rode the first user message, and asks "what would you like me to do?" — stalling the
# run. A user-message preamble cannot reach turn 2; a system-prompt directive is present at every
# turn and holds the contract across tool turns.
_PI_SYSTEM_CONTRACT = (
    "You are a headless worker in an automated pipeline — this holds for EVERY turn, including "
    "after tool calls return. There is no human at the keyboard: never ask a question and wait, "
    "never pause for a nod, never stop to ask 'what would you like me to do?'. Your task is stated "
    "in the first user message; tool output is progress, never a signal that the task is done or "
    "absent. Keep working until the deliverable exists, then emit EXACTLY ONE receipt line as your "
    "final message (DONE: / FAILED: / PASS: / BLOCK:)."
)


def _pi_agent_dir() -> Path:
    """Resolve Pi's config directory using the same override as Pi itself."""
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(configured).expanduser() if configured else _DEFAULT_PI_AGENT_DIR.expanduser()


def _positive_int(value: object) -> int | None:
    """Return a positive integer while rejecting booleans and JSON numeric lookalikes."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _split_provider_model(model: str, providers: set[str]) -> tuple[str, str] | None:
    """Recognize provider-qualified Pi model names without misreading ordinary model IDs."""
    for separator in ("/", ":"):
        provider, found, model_id = model.partition(separator)
        if found and provider in providers and model_id:
            return provider, model_id
    return None


def _configured_child_concurrency(model: str, *, agent_dir: Path | None = None) -> int | None:
    """Read ``subagentConcurrency`` for an exact Pi model, or ``None`` when uncertain."""
    try:
        raw = json.loads(
            ((agent_dir or _pi_agent_dir()) / "models.json").read_text(encoding="utf-8")
        )
        providers = raw.get("providers") if isinstance(raw, dict) else None
        if not isinstance(providers, dict):
            return None

        qualified = _split_provider_model(model, {key for key in providers if isinstance(key, str)})
        selected = (
            [qualified]
            if qualified is not None
            else [(provider, model) for provider in providers if isinstance(provider, str)]
        )
        matches: list[int] = []
        for provider, model_id in selected:
            provider_data = providers.get(provider)
            entries = provider_data.get("models") if isinstance(provider_data, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("id") != model_id:
                    continue
                concurrency = _positive_int(entry.get("subagentConcurrency"))
                if concurrency is None:
                    return None
                matches.append(concurrency)
        return matches[0] if matches and len(set(matches)) == 1 else None
    except (OSError, ValueError, TypeError):
        return None


def _count_root_pi_processes(*, proc_root: Path | None = None) -> int | None:
    """Count this user's live root Pi processes, excluding marked subagent children."""
    if sys.platform != "linux" or not hasattr(os, "getuid"):
        return None
    root = proc_root or _PROC_ROOT
    try:
        uid = os.getuid()
        process_dirs = list(root.iterdir())
    except OSError:
        return None

    count = 0
    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        try:
            if not process_dir.is_dir():
                continue
            if process_dir.stat().st_uid != uid:
                continue
            if (process_dir / "comm").read_text(encoding="utf-8").strip() != "pi":
                continue
            environment = (process_dir / "environ").read_bytes().split(b"\0")
            if b"PI_SUBAGENT_CHILD=1" in environment:
                continue
            count += 1
        except (OSError, UnicodeError):
            # A process may disappear or become unreadable between directory enumeration and read.
            continue
    return count


def extract_pi_receipt(stdout: str) -> str | None:
    """Pull the receipt from a ``pi --mode json`` event stream, or ``None`` if absent.

    Walks the JSON-lines stream, tracking the text of the **last** ``message_end`` event's final
    ``text`` content part. That part is then scanned line by line for the **last** receipt-shaped
    line (``DONE:`` / ``FAILED:`` / ``PASS:`` / ``BLOCK:`` / needs-decision): small models often emit
    a chatty preamble ("Done. File confirmed on disk...") *before* the actual ``DONE:`` receipt in
    the same text part, and ``classify_receipt`` anchors its match at the START of the string — so
    returning the whole part verbatim would misclassify a real DONE as GARBAGE (observed: the commit
    phase failing with its own DONE receipt as the reason). Mirrors opencode's line-scan.

    Tolerant of CRLF, of non-JSON log noise, and of partial streams (a crashed run with no
    ``message_end`` yields ``None`` → GARBAGE upstream).
    """
    part: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message_end":
            continue
        text = _last_text_part(obj.get("message"))
        if text is not None:
            part = text
    if part is None:
        return None
    # Return the last receipt-shaped line within the final text part; fall back to the whole part
    # (preserving the GARBAGE signal) when it contains no receipt line at all.
    receipt_line: str | None = None
    for text_line in part.splitlines():
        if normalized := _normalize_receipt_line(text_line):
            receipt_line = normalized
    return receipt_line if receipt_line is not None else part


def _last_text_part(message: object) -> str | None:
    """Last non-empty ``text`` content part of a pi assistant message, stripped."""
    if not isinstance(message, dict) or message.get("role") not in (None, "assistant"):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    found: str | None = None
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                found = text.strip()
    return found


@register_runner
@dataclass(slots=True)
class PiRunner(Runner):
    """Drive a phase through the ``pi`` CLI."""

    name = "pi"
    supports_session_repair = True

    directory: str
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _sessions: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _sessions_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @override
    def available_session_capacity(self, model: str) -> int:
        """Return currently available root Pi slots for ``model``.

        Pi's model field counts child requests, so one is added for the parent/root request. Root
        Pi processes already alive at this snapshot consume slots; vLLM remains the final queue for
        processes that race in after discovery.
        """
        child_capacity = _configured_child_concurrency(model)
        active_roots = _count_root_pi_processes()
        if child_capacity is None or active_roots is None:
            return 1
        return max(1, child_capacity + 1 - active_roots)

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
        session_id = str(uuid.uuid4())
        with self._sessions_lock:
            self._sessions[(agent, preset)] = session_id
        return self._spawn_in_session(
            agent,
            preset,
            prompt,
            session_id=session_id,
            timeout=timeout,
            stream_path=stream_path,
            on_tool=on_tool,
            on_usage=on_usage,
            abort_reason=abort_reason,
        )

    @override
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
        """Continue the exact Pi conversation created by the latest matching spawn."""
        with self._sessions_lock:
            session_id = self._sessions.get((agent, preset))
        if session_id is None:
            raise SpawnError(f"cannot repair pi:{agent}: its session id is unavailable")
        return self._spawn_in_session(
            agent,
            preset,
            prompt,
            session_id=session_id,
            timeout=timeout,
            stream_path=stream_path,
            on_tool=on_tool,
            on_usage=on_usage,
            abort_reason=abort_reason,
        )

    def _spawn_in_session(
        self,
        agent: str,
        preset: str,
        prompt: str,
        *,
        session_id: str,
        timeout: float,
        stream_path: Path,
        on_tool: Callable[[str], None] | None,
        on_usage: Callable[[LiveUsage], None] | None,
        abort_reason: Callable[[], str | None] | None,
    ) -> str:
        """Run one prompt in a named Pi session, creating or continuing it."""
        # Resolve the real executable: on Windows ``pi`` is a .cmd/.ps1 shim, and subprocess
        # without shell=True can't launch a bare ``pi`` (WinError 2).
        executable = shutil.which("pi")
        if executable is None:
            raise SpawnError("could not launch pi: not found on PATH")
        # ``agent`` (the phase id) is intentionally ignored, exactly as the opencode runner ignores
        # it: quill's persona carries the role and rides the STDIN prompt, not a CLI flag. Passing
        # it to ``--append-system-prompt`` would inject the bare id ("plan"/"impl") as system-prompt
        # text — meaningless, and inconsistent with opencode. See OpencodeRunner.spawn.
        _ = agent
        # The prompt is large (persona + ticket) and is passed on STDIN, not
        # as an argv element: a fat prompt blows the Windows command-line limit, and pi's ``@<file>``
        # syntax hangs under ``-p --mode json`` headless. ``pi -p`` (bare flag) reads the message
        # from stdin (documented: print mode merges piped stdin into the prompt), which handles both.
        #
        # ``--append-system-prompt`` puts the headless contract in the SYSTEM prompt, which pi
        # re-presents on every turn. Without it the "never ask a question" contract lives only in the
        # first user message and a weak model drifts after a read-only tool turn (see
        # _PI_SYSTEM_CONTRACT). The task itself still rides STDIN — only the standing contract is
        # promoted to the system prompt.
        cmd = [
            executable,
            "-p",
            "--mode",
            "json",
            "--model",
            preset,
            "--session-id",
            session_id,
            "--extension",
            str(_VLLM_USAGE_EXTENSION),
            "--append-system-prompt",
            _PI_SYSTEM_CONTRACT,
            "-a",
        ]
        return run_streaming(
            cmd,
            cwd=self.directory,
            stream_path=stream_path,
            agent=f"pi:{agent}",
            timeout=timeout,
            input_text=prompt,
            env={**os.environ, "QUILL_PI_EXTENSION_PATH": str(_VLLM_USAGE_EXTENSION)},
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
        return extract_pi_receipt(stdout)

    @override
    def skill_directive(self, names: list[str]) -> str:
        """pi loads a skill with ``/skill:<name>``. Return one trigger per skill, or ``""``.

        The engine places this BEFORE the TASK line (see ``assemble_prompt``), so the wording says
        "load, then carry out the task below" — not "before you begin". pi expands each trigger into
        the full SKILL.md body inline; keeping the task after that block leaves the imperative at the
        model's generation point instead of a generic skill doc.
        """
        if not names:
            return ""
        triggers = " ".join(f"/skill:{name}" for name in names)
        return f"First load these skills: {triggers}. Then carry out the task below."

    @override
    def preflight(self) -> None:
        if shutil.which("pi") is None:
            raise PreflightError(
                "pi was not found on PATH. Install it (https://pi.dev) and ensure `pi` runs, "
                "then re-run."
            )
