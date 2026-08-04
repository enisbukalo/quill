"""Discovery and editing of the persona and skill libraries (server milestone C).

Both are directories of markdown on the server, shared by every repo. They differ only in shape:

* a **persona** is one ``<name>.md``;
* a **skill** is a directory holding ``SKILL.md``, occasionally with auxiliary files beside it
  (24 of 26 skills are the lone file; ``box3d`` carries a ``wiki/`` subdirectory).

Discovery is deliberately forgiving. It walks every entry under a root, and a file with a broken or
absent frontmatter header degrades to "name from the filename, empty description" rather than
breaking the listing — one bad file must never make ``GET /skills`` fail.

Writes are the opposite: strict. Names arrive over HTTP, so each is validated as a plain slug and
every resulting path is jailed inside the root before anything touches disk.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from quill.frontmatter import split_frontmatter
from quill_api.paths import PathEscape, resolve_within

#: A catalog entry name: a filesystem-safe slug, optionally with a ``.md`` suffix for personas.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

SKILL_FILENAME = "SKILL.md"


class CatalogError(RuntimeError):
    """A catalog operation was refused (bad name, missing entry, already exists)."""


class NotFound(CatalogError):
    """The named persona or skill does not exist."""


class AlreadyExists(CatalogError):
    """A create was asked for a name that is already taken."""


class LibraryUnwritable(CatalogError):
    """The library root is missing or not writable.

    Its own error because it is a *setup* problem, not a bad request: the roots are machine-level
    configuration, and the fix is fixing `$QUILL_PERSONAS_DIR` / `$QUILL_SKILLS_DIR` rather than
    anything about the call.
    """


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One persona or skill, as the catalog lists it."""

    name: str
    description: str = ""
    #: Personas only: the phase type this persona is written for.
    suits: str | None = None


def validate_name(name: str) -> str:
    """Return ``name`` if it is a safe catalog name, else raise."""
    candidate = name.strip()
    if not _NAME_RE.match(candidate) or ".." in candidate:
        raise CatalogError(
            f"invalid name {name!r} — use letters, digits, '.', '_', '-', starting with a "
            "letter or digit."
        )
    return candidate


def _entry_from(text: str, fallback: str) -> CatalogEntry:
    """Build an entry from a file's frontmatter, falling back to its filename."""
    meta, _body = split_frontmatter(text)
    suits = meta.get("suits") or meta.get("type")
    return CatalogEntry(
        name=meta.get("name") or fallback,
        description=meta.get("description", ""),
        suits=suits or None,
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write(path: Path, content: str) -> None:
    """Write ``content`` to ``path``, turning filesystem refusals into a typed error.

    A library root that is missing or read-only is a misconfigured server, and saying so beats
    surfacing a bare ``[Errno 13]`` from three frames down.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise LibraryUnwritable(
            f"could not write {path}: {exc}. Check the library root exists and is writable."
        ) from exc


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        raise LibraryUnwritable(f"could not remove {path}: {exc}") from exc


# -- personas ---------------------------------------------------------------------


def persona_file(root: Path, name: str) -> Path:
    """Path to a persona, jailed inside ``root``. Accepts ``plan`` or ``plan.md``."""
    validated = validate_name(name)
    filename = validated if validated.endswith(".md") else f"{validated}.md"
    try:
        return resolve_within(root, filename)
    except PathEscape as exc:
        raise CatalogError(str(exc)) from exc


def discover_personas(root: Path) -> list[CatalogEntry]:
    """Every ``*.md`` in the persona library, sorted by name."""
    if not root.is_dir():
        return []
    entries = [
        _entry_from(_read(path), path.name) for path in sorted(root.glob("*.md")) if path.is_file()
    ]
    return sorted(entries, key=lambda e: e.name)


def read_persona(root: Path, name: str) -> tuple[CatalogEntry, str]:
    """The entry plus the file's full text (frontmatter included, for round-tripping an edit)."""
    path = persona_file(root, name)
    if not path.is_file():
        raise NotFound(f"no persona named {name!r}")
    text = _read(path)
    return _entry_from(text, path.name), text


def write_persona(root: Path, name: str, body: str, *, create: bool) -> Path:
    """Create or replace a persona; return the file written."""
    path = persona_file(root, name)
    if create and path.exists():
        raise AlreadyExists(f"persona {name!r} already exists")
    if not create and not path.exists():
        raise NotFound(f"no persona named {name!r}")
    _write(path, body)
    return path


def delete_persona(root: Path, name: str) -> Path:
    path = persona_file(root, name)
    if not path.is_file():
        raise NotFound(f"no persona named {name!r}")
    _remove(path)
    return path


# -- skills -----------------------------------------------------------------------


def skill_dir(root: Path, name: str) -> Path:
    """Path to a skill's directory, jailed inside ``root``."""
    try:
        return resolve_within(root, validate_name(name))
    except PathEscape as exc:
        raise CatalogError(str(exc)) from exc


def skill_file(root: Path, name: str, relative: str) -> Path:
    """Path to one file inside a skill, jailed inside that skill's directory."""
    directory = skill_dir(root, name)
    try:
        return resolve_within(directory, relative)
    except PathEscape as exc:
        raise CatalogError(str(exc)) from exc


def discover_skills(root: Path) -> list[CatalogEntry]:
    """Every directory containing a ``SKILL.md``, sorted by name."""
    if not root.is_dir():
        return []
    entries: list[CatalogEntry] = []
    for path in sorted(root.iterdir()):
        manifest = path / SKILL_FILENAME
        if path.is_dir() and manifest.is_file():
            entries.append(_entry_from(_read(manifest), path.name))
    return sorted(entries, key=lambda e: e.name)


def skill_aux_files(root: Path, name: str) -> list[str]:
    """Auxiliary files beside ``SKILL.md``, as paths relative to the skill directory."""
    directory = skill_dir(root, name)
    if not directory.is_dir():
        return []
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path.name != SKILL_FILENAME
    )


def read_skill(root: Path, name: str) -> tuple[CatalogEntry, str, list[str]]:
    """The entry, its ``SKILL.md`` text, and the names of any auxiliary files."""
    manifest = skill_file(root, name, SKILL_FILENAME)
    if not manifest.is_file():
        raise NotFound(f"no skill named {name!r}")
    return _entry_from(_read(manifest), name), _read(manifest), skill_aux_files(root, name)


def write_skill(
    root: Path, name: str, body: str, *, create: bool, files: dict[str, str] | None = None
) -> list[Path]:
    """Create or replace a skill's ``SKILL.md`` (plus any auxiliary files); return what changed."""
    directory = skill_dir(root, name)
    manifest = skill_file(root, name, SKILL_FILENAME)
    if create and directory.exists():
        raise AlreadyExists(f"skill {name!r} already exists")
    if not create and not manifest.is_file():
        raise NotFound(f"no skill named {name!r}")

    _write(manifest, body)
    written = [manifest]
    for relative, content in (files or {}).items():
        path = skill_file(root, name, relative)
        _write(path, content)
        written.append(path)
    return written


def write_skill_file(root: Path, name: str, relative: str, content: str) -> Path:
    """Write one auxiliary file inside an existing skill."""
    if not skill_file(root, name, SKILL_FILENAME).is_file():
        raise NotFound(f"no skill named {name!r}")
    path = skill_file(root, name, relative)
    _write(path, content)
    return path


def delete_skill_file(root: Path, name: str, relative: str) -> Path:
    if relative == SKILL_FILENAME:
        raise CatalogError("delete the skill itself rather than its SKILL.md")
    path = skill_file(root, name, relative)
    if not path.is_file():
        raise NotFound(f"no file {relative!r} in skill {name!r}")
    _remove(path)
    return path


def delete_skill(root: Path, name: str) -> Path:
    directory = skill_dir(root, name)
    if not (directory / SKILL_FILENAME).is_file():
        raise NotFound(f"no skill named {name!r}")
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise LibraryUnwritable(f"could not remove {directory}: {exc}") from exc
    return directory
