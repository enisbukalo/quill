"""Unit tests for the quillfolio.toml schema, loader, and validation (ticket #33)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quill import config as cfg
from quill.config import (
    ConfigInvalid,
    ConfigMissing,
    QuillfolioConfig,
    load_config,
    phase_contract_dependencies,
    slugify,
)
from quill.findings import DEFAULT_BLOCKING_POLICY, BlockingPolicy


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no git remote, default branch falls back to 'develop'."""
    monkeypatch.setattr(cfg, "_detect_repo", lambda _d: None)
    monkeypatch.setattr(cfg, "_detect_default_branch", lambda _d: cfg.DEFAULT_PR_BASE_FALLBACK)


# A minimal-but-valid vault: config + the persona files the LLM phases reference.
_PHASES = """\
[[phase]]
id = "branch"
type = "producer"
persona = "branch.md"
model = "plan-27b"
artifact = "branch.md"
produces_contract = "quill.artifact/v1"

[[phase]]
id = "plan"
type = "producer"
label = "write plan"
persona = "plan.md"
model = "plan-27b"
skills = ["python-pro"]
artifact = "plan.md"
inputs = ["branch"]
max_artifact_chars = 16000
produces_contract = "quill.plan/v1"
accepts_contracts = ["quill.artifact/v1", "quill.review.findings/v1"]

[[phase]]
id = "review_impl"
type = "reviewer"
persona = "review-impl.md"
models = ["gemma", "qwen-27b"]
against = ["plan"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.plan/v1"]

[[phase]]
id = "review_final"
type = "finalizer"
persona = "review-final.md"
model = "qwen-27b"
reconciles = ["review_impl"]
gates = true
retry_budget = 2
on_block = "plan"
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.review.findings/v1"]

[[phase]]
id = "build_test"
type = "mechanical"
step = "build_test"
gates = true
produces_contract = "quill.verification/v1"
"""

_FILLED = (
    """\
[repo]
name = "me/proj"
project_board = "Workbench"
pr_base = "main"
excluded_issue_labels = ["EPIC", "blocked", "epic"]

[runner]
kind = "opencode"

[build]
command = "make"
test = "make test"
log_dir = "logs"

[retries]
default = 1
spawn = 2

[timeouts]
opencode_run_seconds = 900
model_load_seconds = 60

"""
    + _PHASES
)


def _make_vault(directory: Path, config_body: str, personas: tuple[str, ...] = ()) -> None:
    """Write the repo's quillfolio.toml plus persona stubs in the machine-level library.

    The library is ``~/.quill/personas``, which conftest's ``_isolated_home`` has already
    redirected into this test's tmp_path.
    """
    (directory / cfg.CONFIG_FILENAME).write_text(config_body, encoding="utf-8")
    library = cfg.default_personas_root()
    library.mkdir(parents=True, exist_ok=True)
    for name in personas:
        (library / name).write_text("persona body", encoding="utf-8")


def test_blocker_memory_defaults_off_and_uses_machine_state_root(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, ("branch.md", "plan.md", "review-impl.md", "review-final.md"))
    runs_root = tmp_path / "machine-state" / "runs"

    config = load_config(tmp_path, runs_root=runs_root)

    assert config.memory_enabled is False
    assert config.memory_root == runs_root.parent / "memory"


def test_blocker_memory_can_be_enabled_per_repository(tmp_path: Path) -> None:
    body = _FILLED + "\n[memory]\nenabled = true\n"
    _make_vault(tmp_path, body, ("branch.md", "plan.md", "review-impl.md", "review-final.md"))

    config = load_config(tmp_path)

    assert config.memory_enabled is True


_DEFAULT_PERSONAS = ("branch.md", "plan.md", "review-impl.md", "review-final.md")


# -- missing config ---------------------------------------------------------------


def test_no_config_raises_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigMissing) as exc:
        load_config(tmp_path)
    assert "quill --init" in str(exc.value)


# -- config that never touched this machine's disk ---------------------------------


def test_load_config_text_resolves_without_a_config_file(tmp_path: Path) -> None:
    """Text parsing remains usable independently of a config file on disk."""
    library = tmp_path / "lib"
    library.mkdir()
    for name in _DEFAULT_PERSONAS:
        (library / name).write_text("persona body", encoding="utf-8")

    config = cfg.load_config_text(
        _FILLED,
        directory=tmp_path,
        personas_root=library,
        runs_root=tmp_path / "runs",
    )

    assert config.repo == "me/proj"
    assert config.phase_ids[0] == "branch"
    assert config.personas_root == library
    assert not (tmp_path / cfg.CONFIG_FILENAME).exists()


def test_load_config_text_and_load_config_agree(tmp_path: Path) -> None:
    """One validator for both paths: what the CLI accepts, the service accepts."""
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)

    from_file = load_config(tmp_path)
    from_text = cfg.load_config_text(_FILLED, directory=tmp_path)

    assert from_file.phase_set_hash() == from_text.phase_set_hash()
    assert from_file.phases == from_text.phases


def test_load_config_text_rejects_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid, match="not valid TOML"):
        cfg.load_config_text("this is [not toml", directory=tmp_path)


def test_named_workflows_are_independent_and_selectable(tmp_path: Path) -> None:
    named = (
        _FILLED.replace(_PHASES, "")
        + """
[workflows]
default = "ticket"

[workflows.ticket]
label = "New ticket"
mode = "create"
[[workflows.ticket.phase]]
id = "plan"
type = "producer"
persona = "plan.md"
model = "plan-27b"
artifact = "plan.md"
produces_contract = "quill.plan/v1"

[workflows.pr_update]
label = "Update existing PR"
mode = "update"
feedback_after_head = true
resolve_review_threads = true
[[workflows.pr_update.phase]]
id = "plan"
type = "producer"
persona = "plan.md"
model = "plan-27b"
artifact = "update.md"
produces_contract = "quill.update.scope/v1"

[workflows.pr_review]
label = "Pull Request Review"
mode = "review"
[[workflows.pr_review.phase]]
id = "review"
type = "reviewer"
persona = "review-impl.md"
model = "review-27b"
produces_contract = "quill.review.findings/v1"
"""
    )
    _make_vault(tmp_path, named, _DEFAULT_PERSONAS)

    config = load_config(tmp_path)
    update = config.select_workflow("pr_update")

    assert config.workflow_id == "ticket"
    plan = config.phase("plan")
    assert plan is not None and plan.artifact == "plan.md"
    assert update.workflow_id == "pr_update"
    update_plan = update.phase("plan")
    assert update_plan is not None and update_plan.artifact == "update.md"
    update_workflow = update.workflow("pr_update")
    assert update_workflow is not None and update_workflow.feedback_after_head
    assert update.phase_set_hash() != config.phase_set_hash()
    review = config.select_workflow("pr_review")
    review_workflow = review.workflow("pr_review")
    assert review_workflow is not None and review_workflow.mode == "review"


def test_named_workflows_reject_cross_graph_references(tmp_path: Path) -> None:
    named = (
        _FILLED.replace(_PHASES, "")
        + """
[workflows]
default = "ticket"
[workflows.ticket]
mode = "create"
[[workflows.ticket.phase]]
id = "plan"
type = "producer"
persona = "plan.md"
model = "m"
artifact = "plan.md"
produces_contract = "quill.plan/v1"
[workflows.pr_update]
mode = "update"
[[workflows.pr_update.phase]]
id = "review"
type = "reviewer"
persona = "review-impl.md"
model = "m"
against = ["plan"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.plan/v1"]
"""
    )
    _make_vault(tmp_path, named, _DEFAULT_PERSONAS)

    with pytest.raises(ConfigInvalid, match="unknown phase 'plan'"):
        load_config(tmp_path)


def test_named_workflows_reject_mixed_legacy_phases(tmp_path: Path) -> None:
    mixed = _FILLED + "\n[workflows]\ndefault='ticket'\n[workflows.ticket]\nmode='create'\n"
    _make_vault(tmp_path, mixed, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="cannot mix"):
        load_config(tmp_path)


def test_named_workflows_require_defined_default(tmp_path: Path) -> None:
    named = (
        _FILLED.replace(_PHASES, "")
        + """
[workflows]
default = "missing"
[workflows.ticket]
mode = "create"
[[workflows.ticket.phase]]
id = "build"
type = "mechanical"
step = "build"
"""
    )
    _make_vault(tmp_path, named, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="default workflow 'missing'"):
        load_config(tmp_path)


def test_load_config_text_labels_errors_with_its_source(tmp_path: Path) -> None:
    """An upload has no path to name, so the caller supplies the label the user will see."""
    with pytest.raises(ConfigInvalid, match="<request>"):
        cfg.load_config_text(
            "[[phase]]\nid='x'\ntype='mechanical'\n", directory=tmp_path, source="<request>"
        )


# -- loaded run -------------------------------------------------------------------


def test_loaded_run_resolves(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    assert isinstance(config, QuillfolioConfig)
    assert config.repo == "me/proj"
    assert config.pr_base == "main"
    assert config.project_board == "Workbench"
    assert config.excluded_issue_labels == ("epic", "blocked")
    assert config.pr_checks_required is True
    assert config.build_command == "make"
    assert config.test_command == "make test"
    assert config.opencode_run_seconds == 900
    assert config.model_load_seconds == 60
    assert config.phase_ids == [
        "branch",
        "plan",
        "review_impl",
        "review_final",
        "build_test",
    ]


def test_repository_can_permit_an_empty_pr_check_rollup(tmp_path: Path) -> None:
    body = _FILLED.replace(
        'excluded_issue_labels = ["EPIC", "blocked", "epic"]',
        'excluded_issue_labels = ["EPIC", "blocked", "epic"]\npr_checks_required = false',
    )
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)

    assert load_config(tmp_path).pr_checks_required is False


def test_pr_checks_policy_requires_a_boolean(tmp_path: Path) -> None:
    body = _FILLED.replace(
        'excluded_issue_labels = ["EPIC", "blocked", "epic"]',
        'excluded_issue_labels = ["EPIC", "blocked", "epic"]\npr_checks_required = "no"',
    )
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)

    with pytest.raises(ConfigInvalid, match="pr_checks_required must be true or false"):
        load_config(tmp_path)


def test_vllm_service_switch_config_resolves(tmp_path: Path) -> None:
    text = _FILLED.replace(
        'kind = "opencode"',
        '''kind = "opencode"
backend = "vllm"

[runner.vllm]
command = ["sudo", "systemctl", "start"]

[runner.vllm.models]
plan-27b = "plan.service"
gemma = "gemma.service"
qwen-27b = "qwen.service"''',
    )
    _make_vault(tmp_path, text, _DEFAULT_PERSONAS)

    config = load_config(tmp_path)

    assert config.vllm_command == ("sudo", "systemctl", "start")
    assert config.vllm_models["gemma"] == "gemma.service"


def test_vllm_backend_requires_machine_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _FILLED.replace('kind = "opencode"', 'kind = "opencode"\nbackend = "vllm"')
    _make_vault(tmp_path, text, _DEFAULT_PERSONAS)
    monkeypatch.delenv("QUILL_VLLM_URL")

    with pytest.raises(ConfigInvalid, match="requires QUILL_VLLM_URL"):
        load_config(tmp_path)


def test_repository_cannot_override_machine_vllm_url(tmp_path: Path) -> None:
    text = _FILLED.replace(
        'kind = "opencode"',
        'kind = "opencode"\nbackend = "vllm"\nserver_url = "http://public.example:8000"',
    )
    _make_vault(tmp_path, text, _DEFAULT_PERSONAS)

    with pytest.raises(ConfigInvalid, match="runner.server_url is not allowed"):
        load_config(tmp_path)


def test_vllm_model_mapping_requires_command(tmp_path: Path) -> None:
    text = _FILLED.replace(
        'kind = "opencode"',
        '''kind = "opencode"
backend = "vllm"

[runner.vllm.models]
gemma = "gemma.service"''',
    )
    _make_vault(tmp_path, text, _DEFAULT_PERSONAS)

    with pytest.raises(ConfigInvalid, match="requires a non-empty runner.vllm.command"):
        load_config(tmp_path)


def test_phase_fields_resolve(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    plan = config.phase("plan")
    assert plan is not None
    assert plan.type == "producer"
    assert plan.model == "plan-27b"
    assert plan.skills == ("python-pro",)
    assert plan.artifact == "plan.md"
    assert not plan.is_fanout

    review = config.phase("review_impl")
    assert review is not None
    assert review.is_fanout
    assert review.models == ("gemma", "qwen-27b")

    final = config.phase("review_final")
    assert final is not None
    assert final.reconciles == ("review_impl",)
    assert final.gates
    assert final.on_block == ("plan",)


# -- phase contract topology -----------------------------------------------------


def test_every_loaded_phase_requires_an_exact_known_contract(tmp_path: Path) -> None:
    missing = _FILLED.replace('produces_contract = "quill.artifact/v1"\n', "", 1)
    _make_vault(tmp_path, missing, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="phase 'branch' is missing produces_contract"):
        load_config(tmp_path)

    unknown = _FILLED.replace("quill.artifact/v1", "quill.artifact/v999", 1)
    _make_vault(tmp_path, unknown, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="unknown contract specification"):
        load_config(tmp_path)

    malformed = _FILLED.replace("quill.artifact/v1", "Quill Artifact", 1)
    _make_vault(tmp_path, malformed, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="invalid contract identifier"):
        load_config(tmp_path)


def test_contract_kind_must_match_phase_type_and_mechanical_step(tmp_path: Path) -> None:
    wrong_type = _FILLED.replace("quill.artifact/v1", "quill.verification/v1", 1)
    _make_vault(tmp_path, wrong_type, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="type 'producer' cannot produce"):
        load_config(tmp_path)

    wrong_step = _FILLED.replace(
        'step = "build_test"\ngates = true\nproduces_contract = "quill.verification/v1"',
        'step = "build_test"\ngates = true\nproduces_contract = "quill.ci/v1"',
    )
    _make_vault(tmp_path, wrong_step, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="allowed steps: ci_check"):
        load_config(tmp_path)


def test_contract_edges_require_exact_acceptance_without_unused_types(tmp_path: Path) -> None:
    missing = _FILLED.replace('accepts_contracts = ["quill.plan/v1"]', "accepts_contracts = []", 1)
    _make_vault(tmp_path, missing, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="does not accept required contract.*quill.plan/v1"):
        load_config(tmp_path)

    unused = _FILLED.replace(
        'produces_contract = "quill.artifact/v1"',
        'produces_contract = "quill.artifact/v1"\naccepts_contracts = ["quill.plan/v1"]',
        1,
    )
    _make_vault(tmp_path, unused, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="accepts contract.*with no declared edge"):
        load_config(tmp_path)

    duplicate = _FILLED.replace(
        'accepts_contracts = ["quill.plan/v1"]',
        'accepts_contracts = ["quill.plan/v1", "quill.plan/v1"]',
        1,
    )
    _make_vault(tmp_path, duplicate, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="accepts_contracts contains duplicates"):
        load_config(tmp_path)


def test_contract_only_requires_is_ordered_known_and_not_duplicated(tmp_path: Path) -> None:
    valid = _FILLED.replace(
        'step = "build_test"\ngates = true\nproduces_contract = "quill.verification/v1"',
        'step = "build_test"\ngates = true\nproduces_contract = "quill.verification/v1"\nrequires = ["review_final"]\naccepts_contracts = ["quill.review.findings/v1"]',
    )
    _make_vault(tmp_path, valid, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    assert phase_contract_dependencies(config)["build_test"] == ("review_final",)

    unknown = valid.replace('requires = ["review_final"]', 'requires = ["missing"]')
    _make_vault(tmp_path, unknown, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="requires unknown phase 'missing'"):
        load_config(tmp_path)

    forward = _FILLED.replace(
        'produces_contract = "quill.artifact/v1"',
        'produces_contract = "quill.artifact/v1"\nrequires = ["plan"]',
        1,
    )
    _make_vault(tmp_path, forward, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="does not run before"):
        load_config(tmp_path)

    overlap = _FILLED.replace('inputs = ["branch"]', 'inputs = ["branch"]\nrequires = ["branch"]')
    _make_vault(tmp_path, overlap, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="repeats semantic dependencies in requires"):
        load_config(tmp_path)


def test_contract_topology_rejects_duplicate_semantic_edges(tmp_path: Path) -> None:
    duplicate = _FILLED.replace(
        'inputs = ["branch"]',
        'inputs = ["branch"]\nsynthesizes = ["branch"]',
    )
    _make_vault(tmp_path, duplicate, _DEFAULT_PERSONAS)
    with pytest.raises(
        ConfigInvalid,
        match="repeats dependency 'branch' across inputs and synthesizes",
    ):
        load_config(tmp_path)


def test_contract_fields_and_spec_digest_affect_phase_set_hash(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    original = load_config(tmp_path).phase_set_hash()
    changed = _FILLED.replace("quill.artifact/v1", "quill.implementation/v1")
    _make_vault(tmp_path, changed, _DEFAULT_PERSONAS)
    assert load_config(tmp_path).phase_set_hash() != original


def test_concurrent_audits_resolve_with_one_shared_vllm_model(tmp_path: Path) -> None:
    text = _FILLED.replace(
        'kind = "opencode"',
        'kind = "opencode"\nbackend = "vllm"',
    ).replace(
        """persona = "review-impl.md"
models = ["gemma", "qwen-27b"]
against = ["plan"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.plan/v1"]""",
        '''against = ["plan"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.plan/v1"]
[[phase.audits]]
id = "architecture"
label = "Requirements + architecture"
persona = "review-impl-architecture.md"
model = "qwen-27b"

[[phase.audits]]
id = "correctness"
label = "Correctness + lifecycle"
persona = "review-impl-correctness.md"
model = "qwen-27b"''',
    )
    personas = _DEFAULT_PERSONAS + (
        "review-impl-architecture.md",
        "review-impl-correctness.md",
    )
    _make_vault(tmp_path, text, personas)

    phase = load_config(tmp_path).phase("review_impl")

    assert phase is not None
    assert [audit.id for audit in phase.audits] == ["architecture", "correctness"]
    assert {audit.model for audit in phase.audits} == {"qwen-27b"}


def test_concurrent_audits_reject_non_vllm_backend(tmp_path: Path) -> None:
    text = _FILLED.replace(
        """persona = "review-impl.md"
models = ["gemma", "qwen-27b"]
against = ["plan"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.plan/v1"]""",
        '''against = ["plan"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.plan/v1"]
[[phase.audits]]
id = "architecture"
persona = "review-impl-architecture.md"
model = "qwen-27b"

[[phase.audits]]
id = "correctness"
persona = "review-impl-correctness.md"
model = "qwen-27b"''',
    )
    personas = _DEFAULT_PERSONAS + (
        "review-impl-architecture.md",
        "review-impl-correctness.md",
    )
    _make_vault(tmp_path, text, personas)

    with pytest.raises(ConfigInvalid, match="requires runner backend = 'vllm'"):
        load_config(tmp_path)


def test_parallel_producers_and_selective_synthesis_gate_resolve(tmp_path: Path) -> None:
    body = """
[repo]
name = "me/proj"
[runner]
kind = "pi"
backend = "vllm"
[build]
command = "make"
test = "make test"

[[phase]]
id = "requirements"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "requirements.md"
parallel_group = "research"
self_check = true
produces_contract = "quill.research.requirements/v1"
accepts_contracts = ["quill.review.findings/v1"]

[[phase]]
id = "technical"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "technical.md"
parallel_group = "research"
self_check = true
produces_contract = "quill.research.technical/v1"
accepts_contracts = ["quill.review.findings/v1"]

[[phase]]
id = "research_synthesis"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "research.md"
synthesizes = ["requirements", "technical"]
produces_contract = "quill.research.synthesis/v1"
accepts_contracts = ["quill.research.requirements/v1", "quill.research.technical/v1", "quill.review.findings/v1"]

[[phase]]
id = "research_gate"
type = "reviewer"
persona = "review-impl.md"
model = "qwen"
against = ["research_synthesis"]
gates = true
structured_findings = true
on_block = "research_synthesis"
selective_on_block = ["requirements", "technical"]
produces_contract = "quill.review.findings/v1"
accepts_contracts = ["quill.research.synthesis/v1"]
"""
    _make_vault(tmp_path, body, ("research.md", "review-impl.md"))

    config = load_config(tmp_path)

    requirements = config.phase("requirements")
    synthesis = config.phase("research_synthesis")
    gate = config.phase("research_gate")
    assert requirements is not None
    assert synthesis is not None
    assert gate is not None
    assert requirements.parallel_group == "research"
    assert synthesis.synthesizes == (
        "requirements",
        "technical",
    )
    assert gate.selective_on_block == (
        "requirements",
        "technical",
    )

    synthesis_block = """[[phase]]
id = "research_synthesis"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "research.md"
synthesizes = ["requirements", "technical"]
produces_contract = "quill.research.synthesis/v1"
accepts_contracts = ["quill.research.requirements/v1", "quill.research.technical/v1", "quill.review.findings/v1"]

"""
    direct = (
        body.replace(synthesis_block, "")
        .replace('against = ["research_synthesis"]', 'against = ["requirements", "technical"]')
        .replace('on_block = "research_synthesis"\n', "")
        .replace(
            'accepts_contracts = ["quill.research.synthesis/v1"]',
            'accepts_contracts = ["quill.research.requirements/v1", "quill.research.technical/v1"]',
        )
    )
    _make_vault(tmp_path, direct, ("research.md", "review-impl.md"))
    direct_gate = load_config(tmp_path).phase("research_gate")
    assert direct_gate is not None
    assert direct_gate.on_block == ()
    assert direct_gate.against == ("requirements", "technical")

    missing_against = direct.replace(
        'against = ["requirements", "technical"]', 'against = ["requirements"]'
    )
    _make_vault(tmp_path, missing_against, ("research.md", "review-impl.md"))
    with pytest.raises(ConfigInvalid, match="must review every retry lane"):
        load_config(tmp_path)


def test_parallel_producer_group_rejects_mixed_models(tmp_path: Path) -> None:
    body = """
[repo]
name = "me/proj"
[runner]
kind = "pi"
backend = "vllm"
[build]
command = "make"
test = "make test"

[[phase]]
id = "requirements"
type = "producer"
persona = "research.md"
model = "qwen-27b"
artifact = "requirements.md"
parallel_group = "research"

[[phase]]
id = "technical"
type = "producer"
persona = "research.md"
model = "qwen-35b"
artifact = "technical.md"
parallel_group = "research"
"""
    _make_vault(tmp_path, body, ("research.md",))

    with pytest.raises(ConfigInvalid, match="must use one shared model"):
        load_config(tmp_path)


def test_parallel_producer_group_rejects_model_fanout(tmp_path: Path) -> None:
    body = """
[repo]
name = "me/proj"
[runner]
kind = "pi"
backend = "vllm"
[build]
command = "make"
test = "make test"

[[phase]]
id = "requirements"
type = "producer"
persona = "research.md"
models = ["qwen", "gemma"]
artifact = "requirements.md"
parallel_group = "research"

[[phase]]
id = "technical"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "technical.md"
parallel_group = "research"
"""
    _make_vault(tmp_path, body, ("research.md",))

    with pytest.raises(ConfigInvalid, match="exactly one model per lane"):
        load_config(tmp_path)


def test_selective_gate_must_cover_every_lane_without_duplicates(tmp_path: Path) -> None:
    body = """
[repo]
name = "me/proj"
[runner]
kind = "pi"
backend = "vllm"
[build]
command = "make"
test = "make test"

[[phase]]
id = "requirements"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "requirements.md"
parallel_group = "research"

[[phase]]
id = "architecture"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "architecture.md"
parallel_group = "research"

[[phase]]
id = "technical"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "technical.md"
parallel_group = "research"

[[phase]]
id = "synthesis"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "research.md"
synthesizes = ["requirements", "technical"]

[[phase]]
id = "gate"
type = "reviewer"
persona = "review-impl.md"
model = "qwen"
against = ["synthesis"]
gates = true
structured_findings = true
on_block = "synthesis"
selective_on_block = ["requirements", "technical"]
"""
    _make_vault(tmp_path, body, ("research.md", "review-impl.md"))
    with pytest.raises(ConfigInvalid, match="must include every lane"):
        load_config(tmp_path)

    duplicate = body.replace(
        'selective_on_block = ["requirements", "technical"]',
        'selective_on_block = ["requirements", "requirements", "technical"]',
    )
    _make_vault(tmp_path, duplicate, ("research.md", "review-impl.md"))
    with pytest.raises(ConfigInvalid, match="duplicate phase ids"):
        load_config(tmp_path)


def test_plain_gate_cannot_retry_one_parallel_lane(tmp_path: Path) -> None:
    body = """
[repo]
name = "me/proj"
[runner]
kind = "pi"
backend = "vllm"
[build]
command = "make"
test = "make test"

[[phase]]
id = "requirements"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "requirements.md"
parallel_group = "research"

[[phase]]
id = "technical"
type = "producer"
persona = "research.md"
model = "qwen"
artifact = "technical.md"
parallel_group = "research"

[[phase]]
id = "gate"
type = "reviewer"
persona = "review-impl.md"
model = "qwen"
against = ["requirements"]
gates = true
structured_findings = true
on_block = "requirements"
"""
    _make_vault(tmp_path, body, ("research.md", "review-impl.md"))

    with pytest.raises(ConfigInvalid, match="cannot use a parallel lane as a plain on_block"):
        load_config(tmp_path)


def test_model_shorthand_becomes_models(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    plan = config.phase("plan")
    assert plan is not None and plan.models == ("plan-27b",)


# -- build/test/runner halts ------------------------------------------------------


def test_blank_build_command_raises(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED.replace('command = "make"', 'command = ""'), _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="build.command"):
        load_config(tmp_path)


def test_blank_test_command_raises(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED.replace('test = "make test"', 'test = ""'), _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="build.test"):
        load_config(tmp_path)


def test_blank_runner_kind_raises(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED.replace('kind = "opencode"', 'kind = ""'), _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="runner.kind"):
        load_config(tmp_path)


# -- phase validation -------------------------------------------------------------


def test_no_phases_raises(tmp_path: Path) -> None:
    body = '[runner]\nkind = "opencode"\n[build]\ncommand = "make"\ntest = "make test"\n'
    _make_vault(tmp_path, body)
    with pytest.raises(ConfigInvalid, match="no \\[\\[phase\\]\\] entries"):
        load_config(tmp_path)


def test_duplicate_phase_id_raises(tmp_path: Path) -> None:
    dup = _FILLED + '\n[[phase]]\nid = "plan"\ntype = "mechanical"\nstep = "build_test"\n'
    _make_vault(tmp_path, dup, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="duplicate phase id 'plan'"):
        load_config(tmp_path)


def test_unknown_type_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('type = "producer"', 'type = "wizard"')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="unknown type 'wizard'"):
        load_config(tmp_path)


def test_unknown_mechanical_step_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('step = "build_test"', 'step = "teleport"')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="unknown step 'teleport'"):
        load_config(tmp_path)


def test_persona_missing_from_the_library_raises(tmp_path: Path) -> None:
    # Omit branch.md/plan.md from the library the config names.
    _make_vault(tmp_path, _FILLED, ("review-impl.md", "review-final.md"))
    with pytest.raises(ConfigInvalid, match="not in the persona library"):
        load_config(tmp_path)


def test_persona_path_escaping_the_library_raises(tmp_path: Path) -> None:
    """A config can arrive over HTTP, so a traversing persona name must be refused rather than
    read and handed to a model."""
    body = _FILLED.replace('persona = "plan.md"', 'persona = "../../../../etc/passwd"')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="escapes the personas root"):
        load_config(tmp_path)


def test_persona_may_live_in_a_subdirectory_of_the_library(tmp_path: Path) -> None:
    """Jailing rejects escapes, not organisation: a library may group personas into folders."""
    body = _FILLED.replace('persona = "plan.md"', 'persona = "python/plan.md"')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    nested = cfg.default_personas_root() / "python"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "plan.md").write_text("persona body", encoding="utf-8")

    config = load_config(tmp_path)

    plan = config.phase("plan")
    assert plan is not None and plan.persona == "python/plan.md"


def test_producer_missing_model_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('model = "plan-27b"\n', "")
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="missing 'model'"):
        load_config(tmp_path)


def test_finalizer_without_reconciles_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('reconciles = ["review_impl"]\n', "")
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="must list the phases it reconciles"):
        load_config(tmp_path)


def test_finalizer_reconciles_unknown_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('reconciles = ["review_impl"]', 'reconciles = ["ghost"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="reconciles unknown phase 'ghost'"):
        load_config(tmp_path)


def test_finalizer_reconciles_non_reviewer_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('reconciles = ["review_impl"]', 'reconciles = ["plan"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="not a reviewer"):
        load_config(tmp_path)


def test_dangling_on_block_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('on_block = "plan"', 'on_block = "nope"')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="on_block = 'nope'"):
        load_config(tmp_path)


def test_multiple_on_block_targets_raise(tmp_path: Path) -> None:
    body = _FILLED.replace('on_block = "plan"', 'on_block = ["plan", "impl"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)

    with pytest.raises(ConfigInvalid, match="multiple on_block targets"):
        load_config(tmp_path)


def test_forward_on_block_target_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('on_block = "plan"', 'on_block = "build_test"', 1)
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)

    with pytest.raises(ConfigInvalid, match="does not run before it"):
        load_config(tmp_path)


def test_against_parsed(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    phase = config.phase("review_impl")
    assert phase is not None
    assert phase.against == ("plan",)


def test_producer_inputs_and_artifact_limit_parsed(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    phase = config.phase("plan")
    assert phase is not None
    assert phase.inputs == ("branch",)
    assert phase.max_artifact_chars == 16_000


def test_inputs_on_non_producer_raise(tmp_path: Path) -> None:
    body = _FILLED.replace('against = ["plan"]', 'against = ["plan"]\ninputs = ["plan"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="defines inputs but is not a producer"):
        load_config(tmp_path)


def test_input_target_must_precede_producer(tmp_path: Path) -> None:
    body = _FILLED.replace('inputs = ["branch"]', 'inputs = ["review_impl"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="produces no artifact"):
        load_config(tmp_path)


def test_against_unknown_phase_raises(tmp_path: Path) -> None:
    body = _FILLED.replace('against = ["plan"]', 'against = ["ghost"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="reviews against unknown phase 'ghost'"):
        load_config(tmp_path)


def test_against_non_producer_raises(tmp_path: Path) -> None:
    # review_impl produces no artifact; reviewing against it has nothing to read.
    body = _FILLED.replace('against = ["plan"]', 'against = ["review_final"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="produces no artifact"):
        load_config(tmp_path)


def test_against_target_after_reviewer_raises(tmp_path: Path) -> None:
    # 'build_test' runs after review_impl, so its (hypothetical) artifact wouldn't exist yet.
    # Use a producer placed later: move plan's artifact-bearing role won't work, so add a
    # late producer the reviewer points forward at.
    body = _FILLED.replace(
        'id = "build_test"\ntype = "mechanical"\nstep = "build_test"\ngates = true\n',
        'id = "late"\ntype = "producer"\npersona = "plan.md"\nmodel = "m"\nartifact = "late.md"\n',
    ).replace('against = ["plan"]', 'against = ["late"]')
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="does not run before it"):
        load_config(tmp_path)


# -- helpers ----------------------------------------------------------------------


def test_repo_derived_when_name_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_detect_repo", lambda _d: "git@github.com:me/derived.git")
    body = _FILLED.replace('name = "me/proj"\n', "")
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    assert config.repo == "git@github.com:me/derived.git"


def test_retry_budget_default_then_phase(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    final = config.phase("review_final")
    assert final is not None
    # An explicit phase budget is more specific than the workflow-wide default.
    assert config.retry_budget(final) == 2
    assert config.spawn_retries() == 2


def test_retry_budget_uses_global_default_when_phase_omits_it(tmp_path: Path) -> None:
    body = _FILLED.replace("retry_budget = 2\n", "")
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    final = config.phase("review_final")
    assert final is not None
    assert final.retry_budget is None
    assert config.retry_budget(final) == 1


def test_retry_budget_falls_back_to_phase_when_no_default(tmp_path: Path) -> None:
    body = _FILLED.replace("[retries]\ndefault = 1\nspawn = 2\n", "")
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    final = config.phase("review_final")
    assert final is not None
    assert config.retry_budget(final) == 2  # phase's own retry_budget
    assert config.spawn_retries() == cfg.DEFAULT_RETRY


def test_phase_resource_budgets_parse_and_require_positive_integers(tmp_path: Path) -> None:
    body = _FILLED.replace(
        "retry_budget = 2",
        "retry_budget = 2\nmax_artifact_chars = 12000",
    )
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    phase = config.phase("review_final")
    assert phase is not None
    assert phase.max_artifact_chars == 12000

    invalid = body.replace("max_artifact_chars = 12000", "max_artifact_chars = 0")
    _make_vault(tmp_path, invalid, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="max_artifact_chars"):
        load_config(tmp_path)


def test_phase_self_check_is_opt_in_and_rejects_mechanical_phases(tmp_path: Path) -> None:
    body = _FILLED.replace('kind = "opencode"', 'kind = "pi"').replace(
        'artifact = "plan.md"', 'artifact = "plan.md"\nself_check = true'
    )
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    config = load_config(tmp_path)
    plan = config.phase("plan")
    branch = config.phase("branch")
    assert plan is not None
    assert branch is not None
    assert plan.self_check is True
    assert branch.self_check is False

    invalid = _FILLED.replace('kind = "opencode"', 'kind = "pi"').replace(
        'step = "build_test"', 'step = "build_test"\nself_check = true'
    )
    _make_vault(tmp_path, invalid, _DEFAULT_PERSONAS)
    with pytest.raises(ConfigInvalid, match="mechanical phase 'build_test' cannot enable"):
        load_config(tmp_path)


def test_self_check_may_name_a_persona_that_must_exist(tmp_path: Path) -> None:
    body = _FILLED.replace('kind = "opencode"', 'kind = "pi"').replace(
        'artifact = "plan.md"', 'artifact = "plan.md"\nself_check = "self-check-plan.md"'
    )
    _make_vault(tmp_path, body, (*_DEFAULT_PERSONAS, "self-check-plan.md"))
    plan = load_config(tmp_path).phase("plan")
    assert plan is not None
    assert plan.self_check is True
    assert plan.self_check_persona == "self-check-plan.md"

    # `true` keeps the built-in prompt, so no persona is named.
    plain = _FILLED.replace('kind = "opencode"', 'kind = "pi"').replace(
        'artifact = "plan.md"', 'artifact = "plan.md"\nself_check = true'
    )
    _make_vault(tmp_path, plain, _DEFAULT_PERSONAS)
    default_phase = load_config(tmp_path).phase("plan")
    assert default_phase is not None
    assert default_phase.self_check is True
    assert default_phase.self_check_persona is None

    # A named persona that is absent from the library is a config error, not a silent fallback.
    _make_vault(tmp_path, body, _DEFAULT_PERSONAS)
    (cfg.default_personas_root() / "self-check-plan.md").unlink()
    with pytest.raises(ConfigInvalid, match="self_check persona 'self-check-plan.md'"):
        load_config(tmp_path)


def test_phase_set_hash_stable_and_sensitive(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    h1 = load_config(tmp_path).phase_set_hash()
    # Reload unchanged -> same hash.
    assert load_config(tmp_path).phase_set_hash() == h1
    # Reorder two phases -> different hash.
    reordered = _FILLED.replace(
        '[[phase]]\nid = "build_test"\ntype = "mechanical"\nstep = "build_test"\ngates = true\nproduces_contract = "quill.verification/v1"\n',
        "",
    )
    reordered = (
        '[[phase]]\nid = "build_test"\ntype = "mechanical"\nstep = "build_test"\ngates = true\nproduces_contract = "quill.verification/v1"\n'
        + reordered
    )
    _make_vault(tmp_path, reordered, _DEFAULT_PERSONAS)
    assert load_config(tmp_path).phase_set_hash() != h1


def test_pr_checks_policy_affects_phase_set_hash(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _DEFAULT_PERSONAS)
    required = load_config(tmp_path).phase_set_hash()
    optional = _FILLED.replace(
        'excluded_issue_labels = ["EPIC", "blocked", "epic"]',
        'excluded_issue_labels = ["EPIC", "blocked", "epic"]\npr_checks_required = false',
    )
    _make_vault(tmp_path, optional, _DEFAULT_PERSONAS)

    assert load_config(tmp_path).phase_set_hash() != required


def test_slugify() -> None:
    assert slugify("gemma-4-31B-it-Q8 MTP") == "gemma-4-31b-it-q8-mtp"
    assert slugify("Qwen 3.6") == "qwen-3-6"
    assert slugify("--weird__name--") == "weird-name"


# -- [gates] blocking policy ------------------------------------------------------

_GATE_PERSONAS = ("branch.md", "plan.md", "review-impl.md", "review-final.md")


def test_gates_default_to_historic_blocking_behavior(tmp_path: Path) -> None:
    _make_vault(tmp_path, _FILLED, _GATE_PERSONAS)

    config = load_config(tmp_path)

    assert config.gates == DEFAULT_BLOCKING_POLICY
    assert config.gates.retry_mode == "same"


def test_gates_configure_a_converging_policy(tmp_path: Path) -> None:
    body = _FILLED + (
        '\n[gates]\nblocking_severities = ["CRITICAL", "MAJOR"]\n'
        'retry_blocking = "repeat-only"\nfinal_round = ["CRITICAL"]\n'
    )
    _make_vault(tmp_path, body, _GATE_PERSONAS)

    config = load_config(tmp_path)

    assert config.gates == BlockingPolicy(
        initial=frozenset({"CRITICAL", "MAJOR"}),
        retry_mode="repeat-only",
        final=frozenset({"CRITICAL"}),
    )


def test_gates_final_round_defaults_to_the_initial_severities(tmp_path: Path) -> None:
    body = _FILLED + '\n[gates]\nblocking_severities = ["CRITICAL"]\n'
    _make_vault(tmp_path, body, _GATE_PERSONAS)

    config = load_config(tmp_path)

    assert config.gates.final == frozenset({"CRITICAL"})


def test_gates_reject_unknown_retry_mode(tmp_path: Path) -> None:
    body = _FILLED + '\n[gates]\nretry_blocking = "whenever"\n'
    _make_vault(tmp_path, body, _GATE_PERSONAS)

    with pytest.raises(ConfigInvalid, match="retry_blocking"):
        load_config(tmp_path)


def test_gates_reject_unknown_severity(tmp_path: Path) -> None:
    body = _FILLED + '\n[gates]\nblocking_severities = ["SEVERE"]\n'
    _make_vault(tmp_path, body, _GATE_PERSONAS)

    with pytest.raises(ConfigInvalid, match="invalid severity"):
        load_config(tmp_path)


def test_gates_reject_empty_severity_list(tmp_path: Path) -> None:
    body = _FILLED + "\n[gates]\nblocking_severities = []\n"
    _make_vault(tmp_path, body, _GATE_PERSONAS)

    with pytest.raises(ConfigInvalid, match="non-empty array"):
        load_config(tmp_path)
