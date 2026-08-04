"""Unit tests for the minimal frontmatter parser."""

from __future__ import annotations

import pytest

from quill.frontmatter import split_frontmatter, strip_frontmatter


def test_parses_pairs_and_returns_body() -> None:
    meta, body = split_frontmatter("---\nname: plan\ndescription: writes a plan\n---\n\nBody here.")
    assert meta == {"name": "plan", "description": "writes a plan"}
    assert body.strip() == "Body here."


def test_no_header_returns_text_unchanged() -> None:
    text = "Just a persona body.\nWith two lines."
    assert split_frontmatter(text) == ({}, text)


def test_unterminated_header_is_treated_as_body() -> None:
    """Swallowing an unclosed header as metadata would silently delete the persona's content."""
    text = "---\nname: plan\nstill going, never closed"
    assert split_frontmatter(text) == ({}, text)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("name: quoted", {"name": "quoted"}),
        ('name: "double"', {"name": "double"}),
        ("name: 'single'", {"name": "single"}),
        ("name: has: colons: inside", {"name": "has: colons: inside"}),
        ("  name:   padded  ", {"name": "padded"}),
        ("# a comment\nname: kept", {"name": "kept"}),
        ("no-separator-here\nname: kept", {"name": "kept"}),
        ("", {}),
    ],
)
def test_header_line_tolerance(header: str, expected: dict[str, str]) -> None:
    """Discovery walks every file under a root, so one odd line must never break a listing."""
    meta, _ = split_frontmatter(f"---\n{header}\n---\nbody")
    assert meta == expected


def test_strip_returns_body_only() -> None:
    assert strip_frontmatter("---\nname: x\n---\nbody").strip() == "body"


def test_a_horizontal_rule_in_the_body_is_not_a_header() -> None:
    text = "Some prose.\n\n---\n\nMore prose."
    assert split_frontmatter(text) == ({}, text)
