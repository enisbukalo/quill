"""Unit tests for `quill --init` bootstrap (ticket #33)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill import config as cfg
from quill.bootstrap import InitError, init_config, seed_personas
from quill.config import ConfigMissing, load_config


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_detect_repo", lambda _d: "git@github.com:me/proj.git")
    monkeypatch.setattr(cfg, "_detect_default_branch", lambda _d: "main")


_SHIPPED_PERSONAS = {
    "research.md",
    "research-requirements.md",
    "research-architecture.md",
    "research-technical.md",
    "research-synthesis.md",
    "review-research.md",
    "plan.md",
    "impl.md",
    "impl-core.md",
    "impl-integration.md",
    "impl-finalize.md",
    "review-plan.md",
    "review-impl.md",
    "review-impl-architecture.md",
    "review-impl-correctness.md",
    "review-impl-tests.md",
    "review-final.md",
    "commit.md",
}


def test_init_writes_only_the_config_file(tmp_path: Path) -> None:
    """A repo's whole quill surface is one file — personas live in the machine-level library."""
    repo = tmp_path / "repo"
    repo.mkdir()

    written = init_config(repo)

    assert written == repo / cfg.CONFIG_FILENAME
    assert list(repo.iterdir()) == [written]


def test_init_refuses_to_clobber_an_existing_config(tmp_path: Path) -> None:
    init_config(tmp_path)
    with pytest.raises(InitError, match="already exists"):
        init_config(tmp_path)


def test_seed_personas_populates_an_empty_library(tmp_path: Path) -> None:
    root, copied = seed_personas(tmp_path / "personas")
    shipped = _SHIPPED_PERSONAS | {
        "update-scope.md",
        "update-impl.md",
        "review-update.md",
        "pr-review-requirements.md",
        "pr-review-correctness.md",
        "pr-review-architecture.md",
        "pr-review-final.md",
        "self-check-findings.md",
        "self-check-plan.md",
        "self-check-pr-update.md",
        "self-check-research-architecture.md",
        "self-check-research-requirements.md",
        "self-check-research-technical.md",
    }
    assert copied == len(shipped)
    assert {p.name for p in root.glob("*.md")} == shipped


def test_seed_personas_leaves_a_populated_library_alone(tmp_path: Path) -> None:
    """The library is shared by every repo on the machine, so re-seeding must never overwrite it."""
    root = tmp_path / "personas"
    root.mkdir()
    (root / "plan.md").write_text("my edited plan persona", encoding="utf-8")

    _, copied = seed_personas(root)

    assert copied == 0
    assert (root / "plan.md").read_text(encoding="utf-8") == "my edited plan persona"
    assert not (root / "impl.md").exists()


def test_load_before_init_raises_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigMissing):
        load_config(tmp_path)


def test_default_config_loads_after_filling_required_fields(tmp_path: Path) -> None:
    """The shipped default loads once its required build and runner fields are set."""
    config_file = init_config(tmp_path)
    personas_root, _ = seed_personas(tmp_path / "personas")
    text = config_file.read_text(encoding="utf-8")
    text = text.replace('kind = ""', 'kind = "opencode"')
    text = text.replace('command = ""', 'command = "make"')
    text = text.replace('test    = ""', 'test    = "make test"')
    config_file.write_text(text, encoding="utf-8")

    config = load_config(tmp_path, personas_root=personas_root, runs_root=tmp_path / "runs")
    assert config.runner == "opencode"
    assert config.phase_ids == [
        "research_requirements",
        "research_architecture",
        "research_technical",
        "research_synthesis",
        "research_gate",
        "plan",
        "review_plan",
        "impl",
        # Mechanical verification runs before LLM review: a failing test or build is unambiguous
        # and cheap, and reviewers should only spend rounds on code that already compiles.
        "build_test",
        "review_impl",
        "review_impl_final",
        "commit",
    ]
    plan = config.phase("plan")
    assert plan is not None and plan.inputs == ("research_synthesis",)
    # The default implementation review fans out to three concurrent lanes.
    review = config.phase("review_impl")
    assert review is not None and len(review.audits) == 3 and review.structured_findings
    final = config.phase("review_impl_final")
    assert final is not None and final.reconciles == ("review_impl",)


def test_default_config_names_personas_the_seeded_library_provides(tmp_path: Path) -> None:
    """The shipped config and the shipped personas must agree, or every fresh install fails
    validation on its first run."""
    config_file = init_config(tmp_path)
    personas_root, _ = seed_personas(tmp_path / "personas")
    text = config_file.read_text(encoding="utf-8")
    text = text.replace('kind = ""', 'kind = "opencode"')
    text = text.replace('command = ""', 'command = "make"')
    text = text.replace('test    = ""', 'test    = "make test"')
    config_file.write_text(text, encoding="utf-8")

    config = load_config(tmp_path, personas_root=personas_root, runs_root=tmp_path / "runs")

    named = {ph.persona for ph in config.phases if ph.persona}
    assert named <= _SHIPPED_PERSONAS
    for persona in named:
        assert config.persona_path(persona).is_file()
