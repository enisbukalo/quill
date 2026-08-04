"""Minimal YAML-frontmatter parsing for persona and skill files.

Persona and skill markdown carries a small metadata header::

    ---
    name: impl
    description: writes the implementation
    ---
    <body>

Only flat ``key: value`` pairs are used — no nesting, lists, or anchors — so this parses the
header directly instead of pulling in a YAML dependency for four scalar fields.

The contract is deliberately forgiving: a file with no header, an unterminated header, or a
malformed line is *not* an error. Catalog discovery walks every file under a root, and one bad
header must never break the listing or fail a run — the caller falls back to the filename.
"""

from __future__ import annotations

_DELIMITER = "---"


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``text`` into its frontmatter mapping and the body below it.

    Returns ``({}, text)`` unchanged when there is no well-formed header, so a persona without
    one loads exactly as it did before frontmatter existed.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        return {}, text

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _DELIMITER:
            return _parse_pairs(lines[1:index]), "\n".join(lines[index + 1 :])

    # Opened but never closed: treat the whole file as body rather than swallowing it as metadata.
    return {}, text


def strip_frontmatter(text: str) -> str:
    """``text`` with any frontmatter header removed."""
    return split_frontmatter(text)[1]


def _parse_pairs(lines: list[str]) -> dict[str, str]:
    """Flat ``key: value`` pairs from the header's lines; unparsable lines are skipped."""
    pairs: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        key = key.strip()
        if key:
            pairs[key] = value.strip().strip("\"'")
    return pairs
