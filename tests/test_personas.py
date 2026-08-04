"""Unit tests for persona loading (ticket #33)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill.personas import PREAMBLE, PersonaNotFound, load_persona


def test_load_persona_prepends_preamble(tmp_path: Path) -> None:
    persona = tmp_path / "plan.md"
    persona.write_text("Your job: write a plan.\nReceipt: DONE: <x>.", encoding="utf-8")
    out = load_persona(persona)
    assert out.startswith(PREAMBLE)
    assert "Your job: write a plan." in out
    assert "Receipt: DONE: <x>." in out


def test_load_persona_strips_trailing_whitespace(tmp_path: Path) -> None:
    persona = tmp_path / "p.md"
    persona.write_text("body text\n\n\n", encoding="utf-8")
    out = load_persona(persona)
    assert out.endswith("body text")


def test_load_persona_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(PersonaNotFound):
        load_persona(tmp_path / "nope.md")


def test_load_persona_strips_catalog_frontmatter(tmp_path: Path) -> None:
    """Frontmatter describes the persona to `GET /personas`; it is not instructions for the model,
    so it must not reach the prompt."""
    persona = tmp_path / "plan.md"
    persona.write_text(
        "---\nname: plan\ndescription: writes a plan\nsuits: producer\n---\n\nYour job: plan.",
        encoding="utf-8",
    )

    out = load_persona(persona)

    assert "description: writes a plan" not in out
    assert "suits: producer" not in out
    assert out.startswith(PREAMBLE)
    assert out.endswith("Your job: plan.")


def test_no_results_dir_token_substitution(tmp_path: Path) -> None:
    """Personas are path-agnostic: a literal {results_dir} is left untouched, not formatted."""
    persona = tmp_path / "p.md"
    persona.write_text("write to {results_dir}/x.md", encoding="utf-8")
    out = load_persona(persona)
    assert "{results_dir}/x.md" in out
