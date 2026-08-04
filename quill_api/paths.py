"""Path jailing for names that arrive over HTTP.

Persona names, skill names, auxiliary file paths and artifact names are all supplied by callers and
all end up joined onto a server directory. ``resolve_within`` is the single place that decides
whether the result is allowed, so no route re-implements (and mis-implements) the check.

Resolution is done with :meth:`Path.resolve`, which collapses ``..`` **and** follows symlinks — a
symlink inside the root pointing outside it is an escape too, and a purely lexical check would miss
it.
"""

from __future__ import annotations

from pathlib import Path


class PathEscape(ValueError):
    """A supplied name resolved outside the directory it was meant to stay in."""


def resolve_within(root: Path, name: str) -> Path:
    """Resolve ``name`` under ``root``, or raise :class:`PathEscape`.

    Rejects absolute paths, traversal, and anything that resolves outside ``root`` — including
    ``root`` itself, since every caller wants a file *in* the directory, never the directory.
    """
    if not name or name in (".", ".."):
        raise PathEscape("empty path")
    candidate = Path(name)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise PathEscape(f"absolute paths are not allowed: {name!r}")

    root_resolved = root.resolve()
    try:
        target = (root_resolved / candidate).resolve()
    except OSError as exc:
        raise PathEscape(f"could not resolve {name!r}: {exc}") from exc

    if target == root_resolved or root_resolved not in target.parents:
        raise PathEscape(f"{name!r} escapes {root_resolved}")
    return target
