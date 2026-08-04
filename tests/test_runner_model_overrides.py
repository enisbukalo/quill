"""Run-local model override behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill.config import AuditDef, ConfigError, PhaseDef, QuillfolioConfig, WorkflowDef
from quill_api.runner import apply_model_overrides


def _config(tmp_path: Path, phases: list[PhaseDef]) -> QuillfolioConfig:
    return QuillfolioConfig(
        directory=tmp_path,
        repo="me/proj",
        pr_base="main",
        runner="pi",
        build_command="./build.sh --build",
        test_command="./build.sh --test",
        log_dir="logs",
        phases=phases,
        vllm_models={"default": "model-default.service", "alternate": "model-alt.service"},
    )


def test_model_override_replaces_one_phase_without_mutating_config(tmp_path: Path) -> None:
    original = _config(
        tmp_path,
        [PhaseDef(id="plan", type="producer", persona="plan.md", models=("default",))],
    )

    changed = apply_model_overrides(original, (("plan", "alternate"),))

    assert original.phases[0].models == ("default",)
    assert changed.phases[0].models == ("alternate",)


def test_model_override_survives_pipeline_workflow_reselection(tmp_path: Path) -> None:
    phase = PhaseDef(id="plan", type="producer", persona="plan.md", models=("default",))
    original = _config(tmp_path, [phase])
    original.workflows = {
        "ticket": WorkflowDef("ticket", "New ticket", "create", (phase,)),
    }

    changed = apply_model_overrides(original.select_workflow("ticket"), (("plan", "alternate"),))
    reselected = changed.select_workflow("ticket")

    assert reselected.phases[0].models == ("alternate",)
    assert original.workflows["ticket"].phases[0].models == ("default",)


def test_model_override_preserves_concurrent_audit_lanes(tmp_path: Path) -> None:
    original = _config(
        tmp_path,
        [
            PhaseDef(
                id="review",
                type="reviewer",
                audits=(
                    AuditDef("architecture", "Architecture", "arch.md", "default"),
                    AuditDef("correctness", "Correctness", "correct.md", "default"),
                ),
            )
        ],
    )

    changed = apply_model_overrides(original, (("review", "alternate"),))

    assert [audit.id for audit in changed.phases[0].audits] == ["architecture", "correctness"]
    assert {audit.model for audit in changed.phases[0].audits} == {"alternate"}


def test_parallel_producer_override_must_update_every_lane(tmp_path: Path) -> None:
    original = _config(
        tmp_path,
        [
            PhaseDef(
                id=lane,
                type="producer",
                persona=f"{lane}.md",
                models=("default",),
                parallel_group="research",
            )
            for lane in ("requirements", "architecture", "technical")
        ],
    )

    with pytest.raises(ConfigError, match="parallel producer group on one model"):
        apply_model_overrides(original, (("requirements", "alternate"),))

    changed = apply_model_overrides(
        original,
        tuple((lane, "alternate") for lane in ("requirements", "architecture", "technical")),
    )
    assert {phase.model for phase in changed.phases} == {"alternate"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ((("missing", "alternate"),), "unknown phase"),
        ((("plan", "unavailable"),), "unavailable model"),
    ],
)
def test_model_override_rejects_unknown_targets(
    tmp_path: Path, overrides: tuple[tuple[str, str], ...], message: str
) -> None:
    config = _config(
        tmp_path,
        [PhaseDef(id="plan", type="producer", persona="plan.md", models=("default",))],
    )

    with pytest.raises(ConfigError, match=message):
        apply_model_overrides(config, overrides)
