"""Persona and skill library tests — discovery, editing, and path jailing."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill_api import catalog
from quill_api.paths import PathEscape, resolve_within


@pytest.fixture
def personas(tmp_path: Path) -> Path:
    root = tmp_path / "personas"
    root.mkdir()
    (root / "plan.md").write_text(
        "---\nname: plan\ndescription: writes a plan\nsuits: producer\n---\n\nbody",
        encoding="utf-8",
    )
    (root / "bare.md").write_text("no frontmatter here", encoding="utf-8")
    return root


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "cpp-pro").mkdir(parents=True)
    (root / "cpp-pro" / "SKILL.md").write_text(
        "---\nname: cpp-pro\ndescription: modern C++\n---\nbody", encoding="utf-8"
    )
    (root / "box3d" / "wiki").mkdir(parents=True)
    (root / "box3d" / "SKILL.md").write_text("---\nname: box3d\n---\nbody", encoding="utf-8")
    (root / "box3d" / "wiki" / "01-start.md").write_text("wiki page", encoding="utf-8")
    (root / "not-a-skill").mkdir()  # no SKILL.md
    return root


# -- path jail --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["../outside.md", "../../etc/passwd", "/etc/passwd", "", ".", "..", "a/../../b"]
)
def test_resolve_within_rejects_escapes(tmp_path: Path, name: str) -> None:
    with pytest.raises(PathEscape):
        resolve_within(tmp_path, name)


def test_resolve_within_rejects_a_symlink_pointing_outside(tmp_path: Path) -> None:
    """A lexical check would pass this: the path has no '..' in it at all."""
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    (root / "link.md").symlink_to(secret)

    with pytest.raises(PathEscape):
        resolve_within(root, "link.md")


def test_resolve_within_allows_a_subdirectory(tmp_path: Path) -> None:
    assert resolve_within(tmp_path, "wiki/page.md") == (tmp_path / "wiki" / "page.md").resolve()


# -- discovery --------------------------------------------------------------------


def test_discover_personas_reads_frontmatter(personas: Path) -> None:
    entries = {e.name: e for e in catalog.discover_personas(personas)}
    assert entries["plan"].description == "writes a plan"
    assert entries["plan"].suits == "producer"


def test_discovery_survives_a_file_with_no_frontmatter(personas: Path) -> None:
    """One malformed file must never break the whole listing."""
    names = [e.name for e in catalog.discover_personas(personas)]
    assert "bare.md" in names  # falls back to the filename


def test_discover_on_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert catalog.discover_personas(tmp_path / "nope") == []
    assert catalog.discover_skills(tmp_path / "nope") == []


def test_discover_skills_requires_a_manifest(skills: Path) -> None:
    names = [e.name for e in catalog.discover_skills(skills)]
    assert names == ["box3d", "cpp-pro"]
    assert "not-a-skill" not in names


def test_skill_aux_files_are_listed_relative(skills: Path) -> None:
    assert catalog.skill_aux_files(skills, "box3d") == ["wiki/01-start.md"]
    assert catalog.skill_aux_files(skills, "cpp-pro") == []


# -- names ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["../evil", "with/slash", "has space", "", ".hidden", "a" * 200, "..", "x;y"]
)
def test_invalid_catalog_names_are_refused(name: str) -> None:
    with pytest.raises(catalog.CatalogError):
        catalog.validate_name(name)


def test_persona_lookup_accepts_a_name_with_or_without_the_suffix(personas: Path) -> None:
    assert catalog.persona_file(personas, "plan") == catalog.persona_file(personas, "plan.md")


# -- persona CRUD -----------------------------------------------------------------


def test_read_persona_returns_entry_and_full_text(personas: Path) -> None:
    entry, text = catalog.read_persona(personas, "plan")
    assert entry.description == "writes a plan"
    # The full text keeps the frontmatter, so an edit can round-trip it.
    assert text.startswith("---")


def test_read_missing_persona_raises_not_found(personas: Path) -> None:
    with pytest.raises(catalog.NotFound):
        catalog.read_persona(personas, "ghost")


def test_create_persona_then_update_and_delete(personas: Path) -> None:
    path = catalog.write_persona(personas, "fresh", "new body", create=True)
    assert path.read_text(encoding="utf-8") == "new body"

    catalog.write_persona(personas, "fresh", "edited", create=False)
    assert path.read_text(encoding="utf-8") == "edited"

    catalog.delete_persona(personas, "fresh")
    assert not path.exists()


def test_create_persona_refuses_to_clobber(personas: Path) -> None:
    with pytest.raises(catalog.AlreadyExists):
        catalog.write_persona(personas, "plan", "overwrite", create=True)


def test_update_a_missing_persona_raises_not_found(personas: Path) -> None:
    with pytest.raises(catalog.NotFound):
        catalog.write_persona(personas, "ghost", "body", create=False)


def test_persona_write_cannot_escape_the_library(personas: Path) -> None:
    with pytest.raises(catalog.CatalogError):
        catalog.write_persona(personas, "../escaped", "body", create=True)
    assert not (personas.parent / "escaped.md").exists()


# -- skill CRUD -------------------------------------------------------------------


def test_read_skill_returns_body_and_aux_files(skills: Path) -> None:
    entry, body, files = catalog.read_skill(skills, "box3d")
    assert entry.name == "box3d"
    assert "body" in body
    assert files == ["wiki/01-start.md"]


def test_create_skill_with_auxiliary_files(skills: Path) -> None:
    """Most skills are a lone SKILL.md, but box3d and blender carry extra docs — so a create has
    to be able to lay down more than one file."""
    written = catalog.write_skill(
        skills, "new-skill", "manifest", create=True, files={"docs/a.md": "aye"}
    )

    assert len(written) == 2
    assert (skills / "new-skill" / "SKILL.md").read_text(encoding="utf-8") == "manifest"
    assert (skills / "new-skill" / "docs" / "a.md").read_text(encoding="utf-8") == "aye"


def test_create_skill_refuses_to_clobber(skills: Path) -> None:
    with pytest.raises(catalog.AlreadyExists):
        catalog.write_skill(skills, "cpp-pro", "x", create=True)


def test_update_skill_replaces_the_manifest(skills: Path) -> None:
    catalog.write_skill(skills, "cpp-pro", "rewritten", create=False)
    assert (skills / "cpp-pro" / "SKILL.md").read_text(encoding="utf-8") == "rewritten"


def test_delete_skill_removes_the_whole_directory(skills: Path) -> None:
    catalog.delete_skill(skills, "box3d")
    assert not (skills / "box3d").exists()


def test_delete_missing_skill_raises_not_found(skills: Path) -> None:
    with pytest.raises(catalog.NotFound):
        catalog.delete_skill(skills, "ghost")


def test_skill_aux_file_write_and_delete(skills: Path) -> None:
    path = catalog.write_skill_file(skills, "cpp-pro", "notes/x.md", "content")
    assert path.read_text(encoding="utf-8") == "content"

    catalog.delete_skill_file(skills, "cpp-pro", "notes/x.md")
    assert not path.exists()


def test_the_manifest_cannot_be_deleted_as_an_aux_file(skills: Path) -> None:
    """Removing SKILL.md would leave a directory that discovery no longer lists — a skill that
    exists on disk but not in the catalog."""
    with pytest.raises(catalog.CatalogError):
        catalog.delete_skill_file(skills, "cpp-pro", "SKILL.md")


def test_skill_file_write_cannot_escape_the_skill(skills: Path) -> None:
    with pytest.raises(catalog.CatalogError):
        catalog.write_skill_file(skills, "cpp-pro", "../../escaped.md", "body")
    assert not (skills.parent / "escaped.md").exists()


def test_writing_a_file_into_a_missing_skill_raises(skills: Path) -> None:
    with pytest.raises(catalog.NotFound):
        catalog.write_skill_file(skills, "ghost", "a.md", "body")


# -- misconfigured library --------------------------------------------------------


def test_writing_into_an_unwritable_library_is_a_typed_error(tmp_path: Path) -> None:
    """A missing or read-only root is a server setup problem, and saying so beats surfacing a
    bare [Errno 13] from three frames down."""
    root = tmp_path / "readonly"
    root.mkdir()
    root.chmod(0o500)
    try:
        with pytest.raises(catalog.LibraryUnwritable, match="writable"):
            catalog.write_persona(root, "new", "body", create=True)
    finally:
        root.chmod(0o700)


def test_reading_a_missing_library_is_simply_empty(tmp_path: Path) -> None:
    """Discovery must not fail on a server whose library has not been set up yet — the listing is
    how you find that out."""
    assert catalog.discover_personas(tmp_path / "absent") == []
