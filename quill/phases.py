"""Phase runner, receipt classification, and the gate/retry loop (WI-4).

Each pipeline phase spawns one ``opencode run`` worker. The worker writes its full answer
to a results file and returns a **one-line receipt** as the last ``type:"text"`` part of
the JSON stream. This module turns that raw output into a structured :class:`PhaseResult`
the orchestrator can branch on, and drives the revise-then-verify retry loop for gated
phases.

Receipt grammar (plan §4):

* ``DONE: <msg>``                                   — producer finished
* ``FAILED: <msg>``                                 — producer failed
* ``PASS: <msg>`` / ``BLOCK: <msg>``                — reviewer verdict (gated phases)
* ``FAILED: needs decision — <question> | result: <path>`` — headless escape

Anything else that still has a receipt-looking line is GARBAGE; a worker that errored or
timed out before producing any parsable receipt is a CRASH.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from quill.contracts import ContractRef

from quill.live_usage import LiveUsage

# A needs-decision receipt: "FAILED: needs decision — <question> | result: <path>".
# The dash may be em-dash or hyphen; the "| result: <path>" tail is optional.
_NEEDS_DECISION_RE = re.compile(
    r"^FAILED:\s*needs decision\s*[—-]\s*(?P<question>.+?)(?:\s*\|\s*result:\s*(?P<path>\S+))?\s*$",
    re.IGNORECASE,
)
_RECEIPT_RE = re.compile(r"^(?P<verb>DONE|FAILED|PASS|BLOCK):\s*(?P<msg>.*)$")


class Outcome(str, Enum):
    """Classified result of a single phase spawn."""

    DONE = "DONE"
    FAILED = "FAILED"
    PASS = "PASS"
    BLOCK = "BLOCK"
    GARBAGE = "GARBAGE"  # ran, but no parsable receipt
    CRASH = "CRASH"  # errored / timed out before a receipt
    NEEDS_DECISION = "NEEDS_DECISION"
    ESCALATE = "ESCALATE"  # all blockers are decisions, skip research → go to planning


@dataclass(slots=True)
class PhaseResult:
    """Structured outcome of one phase spawn."""

    outcome: Outcome
    message: str = ""
    question: str | None = None  # set when outcome is NEEDS_DECISION
    result_path: str | None = None  # path the worker wrote its full answer to
    raw_receipt: str | None = None  # the exact receipt line, for logging
    #: A configured phase consumes this permission after exhausting its fresh-spawn budget. This
    #: prevents a parent gate from retrying itself because a nested repair phase already failed.
    allow_phase_retry: bool = True
    #: Validated durable handoff published by this exact phase attempt.
    contract_ref: ContractRef | None = None

    @property
    def is_pass(self) -> bool:
        return self.outcome is Outcome.PASS

    @property
    def is_block(self) -> bool:
        return self.outcome is Outcome.BLOCK

    @property
    def needs_decision(self) -> bool:
        return self.outcome is Outcome.NEEDS_DECISION


# A receipt line begins with a known verb (or the needs-decision FAILED subtype). Small models
# often emit the verdict/receipt line and then keep reasoning ("Now let me verify…") in a later
# text part, so the *last* text part is not reliably the receipt. We scan every text line and take
# the LAST one that looks like a receipt — the model's actual verdict, wherever it landed.
_RECEIPT_LINE_RE = re.compile(r"^(?:DONE|FAILED|PASS|BLOCK):", re.IGNORECASE)
_MARKDOWN_RECEIPT_PREFIX_RE = re.compile(r"^(?:(?:[-+*]|>)\s+)")
_RECEIPT_WRAPPERS = (("**", "**"), ("__", "__"), ("```", "```"), ("`", "`"), ("(", ")"))


def _normalize_receipt_line(line: str) -> str | None:
    """Return a canonical receipt after removing harmless whole-line presentation wrappers.

    Normalization is deliberately narrow. It accepts Markdown list/quote prefixes and balanced
    parentheses, emphasis, or code-span wrappers only when the remaining whole line starts with a
    receipt verb. It never searches prose for an embedded verdict and never repairs receipt
    grammar, so ambiguous output remains GARBAGE.
    """
    candidate = line.strip()
    for _ in range(4):
        if _RECEIPT_LINE_RE.match(candidate) or _NEEDS_DECISION_RE.match(candidate):
            return candidate
        if prefix := _MARKDOWN_RECEIPT_PREFIX_RE.match(candidate):
            candidate = candidate[prefix.end() :].strip()
            continue
        unwrapped = False
        for opening, closing in _RECEIPT_WRAPPERS:
            if candidate.startswith(opening) and candidate.endswith(closing):
                inner = candidate[len(opening) : -len(closing)].strip()
                if inner:
                    candidate = inner
                    unwrapped = True
                    break
        if not unwrapped:
            return None
    return candidate if _RECEIPT_LINE_RE.match(candidate) else None


def _text_parts(stdout: str) -> list[str]:
    """Every ``type == "text"`` part's text, in stream order (CRLF- and log-noise-tolerant)."""
    parts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        # opencode shapes seen: {"type":"text","text":...} or {"part":{"type":"text","text":...}}
        part = obj.get("part") if isinstance(obj.get("part"), dict) else obj
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return parts


def extract_receipt(stdout: str) -> str | None:
    """Pull the receipt line from an ``opencode run --format json`` stream.

    opencode emits one JSON object per line. The receipt is a line beginning ``DONE:``/``FAILED:``/
    ``PASS:``/``BLOCK:``. We scan every text part's lines and return the **last** receipt-shaped
    line — a chatty model that emits its verdict and then keeps reasoning still classifies on the
    verdict, not the trailing chatter. Falls back to the last text part (then ``None``) when no
    receipt-shaped line is present, preserving the GARBAGE signal for output with no receipt at all.

    This is opencode's receipt shape and the default extractor for :func:`run_phase`. Other
    runners (see :mod:`quill.runners`) supply their own via ``Runner.extract_receipt``.
    """
    parts = _text_parts(stdout)
    if not parts:
        return None
    receipt_line: str | None = None
    for part in parts:
        for text_line in part.splitlines():
            if normalized := _normalize_receipt_line(text_line):
                receipt_line = normalized
    # No receipt-shaped line anywhere: fall back to the last text part so classify_receipt reports
    # GARBAGE with that text as context (unchanged behavior for genuinely receipt-less output).
    return receipt_line if receipt_line is not None else parts[-1]


def classify_receipt(receipt: str | None) -> PhaseResult:
    """Classify a single receipt line into a :class:`PhaseResult`.

    ``None`` (no receipt at all) is GARBAGE — the spawn ran but produced nothing parsable.
    A genuine run error / timeout is a CRASH and is constructed directly by the runner, not
    here.
    """
    if receipt is None:
        return PhaseResult(Outcome.GARBAGE, message="no receipt in worker output")

    original = receipt.strip()
    receipt = _normalize_receipt_line(original) or original

    # needs-decision is a FAILED subtype; check it before the generic FAILED.
    nd = _NEEDS_DECISION_RE.match(receipt)
    if nd:
        return PhaseResult(
            Outcome.NEEDS_DECISION,
            message=receipt,
            question=nd.group("question").strip(),
            result_path=nd.group("path"),
            raw_receipt=receipt,
        )

    m = _RECEIPT_RE.match(receipt)
    if not m:
        return PhaseResult(Outcome.GARBAGE, message=receipt, raw_receipt=receipt)

    verb = m.group("verb").upper()
    msg = m.group("msg").strip()
    result_path = _result_path_from(msg)
    return PhaseResult(Outcome(verb), message=msg, result_path=result_path, raw_receipt=receipt)


def _result_path_from(msg: str) -> str | None:
    """Extract a trailing ``result: <path>`` hint from a receipt message, if present."""
    m = re.search(r"result:\s*(\S+)", msg)
    return m.group(1) if m else None


def classify_output(stdout: str) -> PhaseResult:
    """Convenience: extract the receipt from a raw opencode stream and classify it."""
    return classify_receipt(extract_receipt(stdout))


# -- phase runner -----------------------------------------------------------------


class ModelLoaderLike(Protocol):
    """The pre-spawn model-server seam: ready the server for a phase, and tear it down at run end.

    Both backends satisfy it — :class:`quill.loader.ModelLoader` (llama.cpp preset swap) and
    :class:`quill.modelserver.VllmServer` (always-on; optional prefix reset). ``run_phase`` calls
    ``load`` before every spawn; the CLI calls ``unload_all`` once on exit.
    """

    def load(self, preset: str, timeout: float = ...) -> None: ...

    def unload_all(self) -> None: ...


class Spawner(Protocol):
    """Callable that runs one worker and returns its stdout.

    The real implementations live in :mod:`quill.runners` (each ``Runner.spawn``); tests
    inject a plain function. Must raise :class:`SpawnTimeout` on hang and :class:`SpawnError`
    on a non-zero/abnormal exit so the runner can classify those as CRASH.

    ``stream_path`` is the run-dir file the runner tees the worker's live JSONL stream to (flushed
    per line). It is always provided by the engine; test spawners may ignore it.

    ``on_tool`` is called with each tool's name as the worker starts it (live progress). It is
    keyword-only with a default so a spawner that doesn't care — every test fake — stays a plain
    two-line function.
    """

    def __call__(
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
    ) -> str: ...


#: Receipt extractor signature: raw stdout -> the one-line receipt (or ``None``).
ReceiptExtractor = Callable[[str], "str | None"]


class SpawnError(RuntimeError):
    """The worker process errored (non-zero exit, missing binary)."""


class SpawnTimeout(SpawnError):
    """The worker exceeded its per-spawn timeout (CLIs have no native timeout)."""


def run_phase(
    *,
    loader: ModelLoaderLike,
    spawn: Spawner,
    preset: str,
    agent: str,
    prompt: str,
    timeout: float,
    stream_path: Path,
    load_timeout: float = 180,
    extract: ReceiptExtractor = extract_receipt,
    on_tool: Callable[[str], None] | None = None,
    on_usage: Callable[[LiveUsage], None] | None = None,
    on_model_loaded: Callable[[], None] | None = None,
    abort_reason: Callable[[], str | None] | None = None,
) -> PhaseResult:
    """Load ``preset`` then spawn ``agent`` and classify the result.

    A model-load failure or a spawn error/timeout becomes a CRASH (the driver re-spawns per
    its ``[retries].spawn`` budget); a clean run is classified from its receipt.

    ``extract`` is the runner-specific receipt parser (``Runner.extract_receipt``); it
    defaults to opencode's :func:`extract_receipt` so a plain function spawner still works.

    ``on_tool`` is forwarded to the spawner for live tool-call progress.
    """
    try:
        loader.load(preset, load_timeout)
    except Exception as exc:  # noqa: BLE001 - model backends expose different failure types
        return PhaseResult(Outcome.CRASH, message=f"model load failed: {exc}")

    return run_preloaded_phase(
        spawn=spawn,
        preset=preset,
        agent=agent,
        prompt=prompt,
        timeout=timeout,
        stream_path=stream_path,
        extract=extract,
        on_tool=on_tool,
        on_usage=on_usage,
        on_model_loaded=on_model_loaded,
        abort_reason=abort_reason,
    )


def run_preloaded_phase(
    *,
    spawn: Spawner,
    preset: str,
    agent: str,
    prompt: str,
    timeout: float,
    stream_path: Path,
    extract: ReceiptExtractor = extract_receipt,
    on_tool: Callable[[str], None] | None = None,
    on_usage: Callable[[LiveUsage], None] | None = None,
    on_model_loaded: Callable[[], None] | None = None,
    abort_reason: Callable[[], str | None] | None = None,
) -> PhaseResult:
    """Spawn and classify one worker after its caller prepared the shared model."""
    if on_model_loaded is not None:
        on_model_loaded()
    try:
        if abort_reason is None:
            stdout = spawn(
                agent,
                preset,
                prompt,
                timeout=timeout,
                stream_path=stream_path,
                on_tool=on_tool,
                on_usage=on_usage,
            )
        else:
            stdout = spawn(
                agent,
                preset,
                prompt,
                timeout=timeout,
                stream_path=stream_path,
                on_tool=on_tool,
                on_usage=on_usage,
                abort_reason=abort_reason,
            )
    except SpawnTimeout as exc:
        return PhaseResult(Outcome.CRASH, message=str(exc))
    except SpawnError as exc:
        return PhaseResult(Outcome.CRASH, message=str(exc))

    return classify_receipt(extract(stdout))


# -- gate + revise-then-verify retry ----------------------------------------------


@dataclass(slots=True)
class GateResult:
    """Outcome of a gated phase after its initial review + any revise/verify rounds."""

    passed: bool
    attempts: int  # how many revise/verify rounds ran (0 = initial review passed)
    final: PhaseResult


def run_gate(
    *,
    initial: PhaseResult,
    revise: Callable[[int], PhaseResult],
    verify: Callable[[int], PhaseResult],
    max_retries: int,
) -> GateResult:
    """Drive the revise-then-verify loop for one gated phase (plan §5).

    ``initial`` is the first review's result. On PASS, returns immediately. On BLOCK, runs up
    to ``max_retries`` rounds of: ``revise(attempt)`` (re-spawn the producer with findings)
    then ``verify(attempt)`` (re-spawn the reviewer in narrow verification mode). A verify
    PASS continues; exhausting the budget halts with the last BLOCK.

    ``max_retries == 0`` means a BLOCK halts immediately (no revise).
    A non-PASS/BLOCK ``initial`` is returned as-is. During revision, verification runs only after
    a successful DONE/PASS repair route; every other outcome is terminal and remains unresolved.
    """
    if initial.outcome not in (Outcome.PASS, Outcome.BLOCK):
        return GateResult(passed=False, attempts=0, final=initial)
    if initial.is_pass:
        return GateResult(passed=True, attempts=0, final=initial)

    last = initial
    for attempt in range(1, max_retries + 1):
        revised = revise(attempt)
        # Verification is meaningful only after the entire repair route completed successfully.
        # FAILED, BLOCK, CRASH, GARBAGE, and NEEDS_DECISION are all terminal here; continuing to
        # verify after any of them can falsely pass work that the repair phase did not complete.
        if revised.outcome not in (Outcome.DONE, Outcome.PASS):
            return GateResult(passed=False, attempts=attempt, final=revised)
        verdict = verify(attempt)
        if verdict.is_pass:
            return GateResult(passed=True, attempts=attempt, final=verdict)
        if verdict.outcome not in (Outcome.PASS, Outcome.BLOCK):
            return GateResult(passed=False, attempts=attempt, final=verdict)
        last = verdict

    return GateResult(passed=False, attempts=max_retries, final=last)
