"""Persona loading + the universal headless preamble (ticket #33).

Personas are no longer hardcoded strings. Each phase names a persona ``.md`` file under the
repo's ``quillvault/`` (e.g. ``personas/plan.md``); this module reads that file and prepends
:data:`PREAMBLE`, the headless contract shared by every phase. Personas are **path-agnostic** —
they carry no ``{results_dir}`` token. The engine states the run dir + the artifact / findings
paths in the assembled prompt (see :func:`quill.engine.assemble_prompt`) before spawning, so the
substitution surface here is zero: a persona is loaded verbatim under the preamble.
"""

from __future__ import annotations

from pathlib import Path

from quill.frontmatter import strip_frontmatter

# Universal preamble prepended to every phase — the headless contract. No human is present, so
# the worker must never block on a question.
PREAMBLE = (
    "You are a headless worker in an automated pipeline. There is no human at the keyboard: "
    "never ask a question and wait, never pause for a nod. Read the repository as needed to "
    "ground every claim in real `file:line` — do not guess at APIs, signatures, or behavior. "
    "Return EXACTLY ONE receipt line as your final message, "
    "in the grammar your task specifies (DONE: / FAILED: / PASS: / BLOCK:)."
)


class PersonaNotFound(RuntimeError):
    """A phase references a persona file that doesn't exist under the vault."""


def load_persona(path: str | Path) -> str:
    """Return ``PREAMBLE`` + the body of the persona file at ``path``.

    ``path`` is the fully-resolved path to the persona ``.md`` (the engine joins the personas root
    to the phase's ``persona``). Config validation already checked the file exists, but we raise
    :class:`PersonaNotFound` defensively so a race / deletion surfaces clearly rather than as an
    empty prompt.

    Any frontmatter header is stripped: it is catalog metadata for ``GET /personas``, and feeding
    ``name:``/``description:`` lines to the model would put a stray doc header at the top of every
    prompt.
    """
    return PREAMBLE + "\n\n" + load_persona_body(path)


def load_persona_body(path: str | Path) -> str:
    """Return the persona body at ``path`` without :data:`PREAMBLE`.

    Used for same-session continuations such as the self-check, where the worker already carries
    the preamble from the phase's opening prompt. Repeating it would spend context restating a
    contract the session is already under.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaNotFound(f"persona file not readable: {p} ({exc})") from exc
    return strip_frontmatter(text).strip()
