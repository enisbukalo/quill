"""`quillfolio.toml` schema, loader, and validation (ticket #33).

quill is a **data-driven phase engine**. A target repo carries its whole pipeline definition in a
single ``quillfolio.toml`` at its root: an ordered ``[[phase]]`` array plus repo / runner / build /
timeout settings. This module is the single seam between that file and the engine:

* :func:`load_config` resolves the file into a :class:`QuillfolioConfig` the engine consumes.
* :func:`load_config_text` provides the shared parser/validator used by file loading and tests.
* The config is validated up front (:func:`_validate_phases`) and fails fast with a specific
  message — never a mid-run crash on a typo.
* :meth:`QuillfolioConfig.phase_set_hash` is a stable fingerprint of the resolved phase set,
  stored in a run's state file so ``--resume`` can refuse to resume across a config change.

The config file is the repo's **entire** quill surface. Personas resolve from a machine-level
library (:attr:`QuillfolioConfig.personas_root`) and run artifacts land in a machine-level runs
root, so one persona library serves every repo and nothing quill writes lands in the checkout.
A repo with no ``quillfolio.toml`` raises :class:`ConfigMissing`, telling the user to run
``quill --init`` (see :mod:`quill.bootstrap`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from quill.contracts import ContractError, default_catalog
from quill.findings import (
    DEFAULT_BLOCKING_POLICY,
    RETRY_MODES,
    SEVERITIES,
    BlockingPolicy,
)

#: The per-repo config file, at the repo root. This is the repo's ENTIRE quill surface: personas
#: and run artifacts are machine-level (see below), so a repo carries one file and nothing else.
CONFIG_FILENAME = "quillfolio.toml"

#: Machine-level state root. Personas are a shared library — the same ``impl.md`` serves every
#: repo, and *which* one a repo wants is a config choice, not a per-repo copy. Run artifacts live
#: here too rather than in the checkout: a server resets and cleans workspaces between runs, and
#: artifacts inside the tree are untracked files a commit phase could sweep into a PR.
STATE_DIRNAME = ".quill"

# Built-in fallbacks. Safe to default — unlike build/test, which are never guessed.
DEFAULT_LOG_DIR = "logs"
DEFAULT_PR_BASE_FALLBACK = "develop"
DEFAULT_RETRY = 1
DEFAULT_OPENCODE_RUN_SECONDS = 5400
DEFAULT_MODEL_LOAD_SECONDS = 360
#: How long a ci_check phase waits for GitHub Actions to finish. Far longer than a local build:
#: it covers queueing on GitHub's runners as well as the run itself.
DEFAULT_CI_SECONDS = 1800

#: Model-server backends the CLI can wire behind the pre-spawn seam (see :mod:`quill.modelserver`).
BACKENDS = ("llamacpp", "vllm")
DEFAULT_BACKEND = "llamacpp"

#: Backends that are **self-hosted**: their real per-token cost is electricity, not an API bill, so
#: the service prices their tokens from local power (``QUILL_USD_PER_1M_TOKENS``) rather than trust
#: the agent CLI's cost field. A future hosted backend would be absent here and keep its CLI cost.
LOCAL_BACKENDS = frozenset({"llamacpp", "vllm"})

#: The phase ``type`` values the engine knows how to dispatch.
PHASE_TYPES = ("producer", "reviewer", "finalizer", "mechanical")


#: The built-in ``step`` names a ``mechanical`` phase may reference.
MECHANICAL_STEPS = (
    "build",
    "test",
    "build_test",
    "ci_check",
    "pr_head_guard",
    "acknowledge_pr_feedback",
    "collect_pr_evidence",
    "publish_pr_review",
)


def default_state_dir() -> Path:
    """``~/.quill`` — resolved per call so tests and the service can redirect it via ``$HOME``."""
    return Path.home() / STATE_DIRNAME


def default_personas_root() -> Path:
    """``~/.quill/personas`` — the shared persona library (symlinked to a config repo)."""
    return default_state_dir() / "personas"


def default_runs_root() -> Path:
    """``~/.quill/runs`` — parent of every ``<run-id>/`` artifact directory."""
    return default_state_dir() / "runs"


def default_memory_root() -> Path:
    """``~/.quill/memory`` — repository-scoped verified blocker history."""
    return default_state_dir() / "memory"


class ConfigError(RuntimeError):
    """Base class for config halts. Always exit non-zero on these."""


class ConfigMissing(ConfigError):
    """No ``quillfolio.toml`` in the target repo — the user must run ``quill --init``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        super().__init__(
            f"no {CONFIG_FILENAME} in {directory} — run `quill --init` to create one, then re-run."
        )


class ConfigInvalid(ConfigError):
    """The loaded ``quillfolio.toml`` is missing or malformed in a way the engine can't run."""


@dataclass(slots=True, frozen=True)
class AuditDef:
    """One independently prompted lane in a concurrent reviewer phase."""

    id: str
    label: str
    persona: str
    model: str
    skills: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class PhaseDef:
    """One resolved phase from the ``[[phase]]`` array.

    The engine dispatches on :attr:`type`. Not every field applies to every type — validation
    (:func:`_validate_phases`) enforces the per-type requirements, so the engine can trust that
    e.g. a ``producer`` has a ``persona`` + a single ``model``, a ``finalizer`` has ``reconciles``.
    """

    id: str
    type: str
    label: str = ""
    persona: str | None = None  # name within the persona library, e.g. "plan.md"
    skills: tuple[str, ...] = ()  # skill NAMES the runner is told to load (quill stores no bodies)
    models: tuple[str, ...] = ()  # one entry => single model; >1 => reviewer fan-out
    artifact: str | None = None  # producer's output filename within the run dir
    step: str | None = None  # mechanical: which built-in step
    gates: bool = False
    structured_findings: bool = False  # engine validates JSON and computes LLM gate verdict
    retry_budget: int | None = None
    max_artifact_chars: int | None = None  # hard handoff-size ceiling; repaired in-session
    self_check: bool = False  # one same-session completion audit after DONE/PASS
    #: Persona file driving that audit. ``self_check = true`` uses the built-in generic prompt;
    #: ``self_check = "self-check-plan.md"`` names a persona so the check is tuned per phase.
    self_check_persona: str | None = None
    parallel_group: str | None = None  # consecutive producer phases executed concurrently
    #: Phases to re-run, in order, when this gate BLOCKs. A list because a revise is not always
    #: one phase: a CI gate must re-run `impl` to fix the code AND `commit` to push it, or the
    #: re-check reads the same commit's status and the retry budget burns without testing
    #: anything. A bare string is accepted and means a one-phase revise.
    on_block: tuple[str, ...] = ()
    #: Optional producer lanes a structured gate may selectively rerun from finding ``owner``.
    #: ``on_block`` names the synthesis phase that runs after the selected lanes finish.
    selective_on_block: tuple[str, ...] = ()
    reconciles: tuple[str, ...] = ()  # finalizer: phase ids whose findings it reconciles
    against: tuple[str, ...] = ()  # reviewer: producer phase ids whose artifacts it reviews against
    inputs: tuple[str, ...] = ()  # producer: earlier artifact-bearing phases it must read
    synthesizes: tuple[
        str, ...
    ] = ()  # producer: latest artifacts combined into one canonical handoff
    audits: tuple[AuditDef, ...] = ()  # reviewer: concurrent, same-model read-only audit lanes
    #: Exact durable output contract. Config loading requires it even though direct test
    #: construction may leave it empty to exercise isolated legacy-free engine helpers.
    produces_contract: str = ""
    #: Exact kinds/versions this phase can consume across every declared forward and retry edge.
    accepts_contracts: tuple[str, ...] = ()
    #: Contract-only dependencies not already represented by inputs/against/reconciles/retry.
    requires: tuple[str, ...] = ()

    @property
    def model(self) -> str | None:
        """The single model for a non-fan-out phase (first of :attr:`models`)."""
        return self.models[0] if self.models else None

    @property
    def is_fanout(self) -> bool:
        return len(self.models) > 1


@dataclass(slots=True, frozen=True)
class WorkflowDef:
    """One named, independently validated phase graph from ``[workflows.<id>]``."""

    id: str
    label: str
    mode: str
    phases: tuple[PhaseDef, ...]
    feedback_after_head: bool = False
    resolve_review_threads: bool = False


@dataclass(slots=True)
class QuillfolioConfig:
    """Resolved, validated config the engine consumes."""

    directory: Path
    repo: str
    pr_base: str
    runner: str
    build_command: str
    test_command: str
    log_dir: str
    phases: list[PhaseDef]
    workflows: dict[str, WorkflowDef] = field(default_factory=dict)
    workflow_id: str = "ticket"
    backend: str = DEFAULT_BACKEND
    vllm_url: str = ""
    vllm_command: tuple[str, ...] = ()
    vllm_models: dict[str, str] = field(default_factory=dict)
    project_board: str | None = None
    excluded_issue_labels: tuple[str, ...] = ()
    retries: dict[str, int] = field(default_factory=dict)
    #: Which findings stop a gate, per revise round. Defaults to historic CRITICAL/MAJOR-always.
    gates: BlockingPolicy = DEFAULT_BLOCKING_POLICY
    opencode_run_seconds: int = DEFAULT_OPENCODE_RUN_SECONDS
    model_load_seconds: int = DEFAULT_MODEL_LOAD_SECONDS
    ci_seconds: int = DEFAULT_CI_SECONDS
    memory_enabled: bool = False
    #: Where ``phase.persona`` names resolve. Machine-level, not per-repo — the service points this
    #: at its shared library and the CLI defaults to ``~/.quill/personas``.
    personas_root: Path = field(default_factory=default_personas_root)
    #: Parent of each run's ``<run-id>/`` artifact directory. Deliberately outside the checkout.
    runs_root: Path = field(default_factory=default_runs_root)
    memory_root: Path = field(default_factory=default_memory_root)

    def persona_path(self, persona: str) -> Path:
        """Absolute path to ``persona`` under :attr:`personas_root`, or raise if it escapes.

        The name comes from a config that, on the server, arrived over HTTP — so a
        ``../../etc/passwd`` would otherwise be read and handed to a model verbatim. Resolving and
        re-checking containment is the jail.
        """
        root = self.personas_root.resolve()
        try:
            target = (root / persona).resolve()
        except OSError as exc:
            raise ConfigInvalid(f"persona path '{persona}' could not be resolved: {exc}") from exc
        if target != root and root not in target.parents:
            raise ConfigInvalid(f"persona path '{persona}' escapes the personas root.")
        return target

    def phase(self, phase_id: str) -> PhaseDef | None:
        """The phase with id ``phase_id``, or ``None``."""
        for ph in self.phases:
            if ph.id == phase_id:
                return ph
        return None

    def workflow(self, workflow_id: str) -> WorkflowDef | None:
        """The named workflow, or ``None`` when this configuration does not define it."""
        if not self.workflows and workflow_id == "ticket":
            return WorkflowDef("ticket", "New ticket", "create", tuple(self.phases))
        return self.workflows.get(workflow_id)

    def select_workflow(self, workflow_id: str) -> QuillfolioConfig:
        """Return a shallow config view whose phase helpers address ``workflow_id``."""
        workflow = self.workflow(workflow_id)
        if workflow is None:
            raise ConfigInvalid(
                f"unknown workflow '{workflow_id}' (choose from: {', '.join(self.workflows)})."
            )
        return replace(self, phases=list(workflow.phases), workflow_id=workflow.id)

    @property
    def phase_ids(self) -> list[str]:
        return [ph.id for ph in self.phases]

    def retry_budget(self, phase: PhaseDef) -> int:
        """Retries for a gated ``phase`` — its own ``retry_budget``, else ``[retries].default``."""
        if phase.retry_budget is not None:
            return phase.retry_budget
        return self.retries.get("default", DEFAULT_RETRY)

    def spawn_retries(self) -> int:
        """Re-spawns on CRASH/GARBAGE for any phase — ``[retries].spawn`` else built-in."""
        return self.retries.get("spawn", DEFAULT_RETRY)

    def phase_set_hash(self) -> str:
        """Stable fingerprint of the resolved phase set, for the ``--resume`` config guard.

        Hashes the ordered phase definitions (the flow-shaping fields). Two configs with the
        same phases in the same order produce the same hash; any add/remove/reorder/retune of a
        phase changes it, so a resume across a config change is refused (see #33 decision 5).
        """
        workflow = self.workflows.get(self.workflow_id)
        payload: object = {
            "workflow": self.workflow_id,
            "memory_enabled": self.memory_enabled,
            "mode": workflow.mode if workflow is not None else "create",
            "feedback_after_head": workflow.feedback_after_head if workflow is not None else False,
            "resolve_review_threads": (
                workflow.resolve_review_threads if workflow is not None else False
            ),
            "phases": [
                {
                    "id": ph.id,
                    "type": ph.type,
                    "label": ph.label,
                    "persona": ph.persona,
                    "skills": list(ph.skills),
                    "models": list(ph.models),
                    "artifact": ph.artifact,
                    "step": ph.step,
                    "gates": ph.gates,
                    "structured_findings": ph.structured_findings,
                    "retry_budget": ph.retry_budget,
                    "max_artifact_chars": ph.max_artifact_chars,
                    "self_check": ph.self_check,
                    "parallel_group": ph.parallel_group,
                    "on_block": list(ph.on_block),
                    "selective_on_block": list(ph.selective_on_block),
                    "reconciles": list(ph.reconciles),
                    "against": list(ph.against),
                    "inputs": list(ph.inputs),
                    "synthesizes": list(ph.synthesizes),
                    "produces_contract": ph.produces_contract,
                    "accepts_contracts": list(ph.accepts_contracts),
                    "requires": list(ph.requires),
                    "contract_spec_digest": (
                        default_catalog().resolve(ph.produces_contract).digest
                        if ph.produces_contract
                        else ""
                    ),
                    "audits": [
                        {
                            "id": audit.id,
                            "label": audit.label,
                            "persona": audit.persona,
                            "model": audit.model,
                            "skills": list(audit.skills),
                        }
                        for audit in ph.audits
                    ],
                }
                for ph in self.phases
            ],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -- git detection ----------------------------------------------------------------


def _git(directory: Path, *args: str) -> str | None:
    """Run a git command in ``directory``; return stripped stdout, or None on failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = out.stdout.strip()
    return value or None


def _detect_repo(directory: Path) -> str | None:
    return _git(directory, "remote", "get-url", "origin")


def _detect_default_branch(directory: Path) -> str:
    """origin's default branch (e.g. ``main``), falling back to ``develop``."""
    ref = _git(directory, "symbolic-ref", "refs/remotes/origin/HEAD")
    if ref:
        return ref.rsplit("/", 1)[-1]
    return DEFAULT_PR_BASE_FALLBACK


# -- parsing ----------------------------------------------------------------------


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a TOML value into a tuple of strings (lenient on shape)."""
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if isinstance(value, str):
        return (value,)
    return ()


def _parse_int_section(raw: dict[str, object], name: str) -> dict[str, int]:
    section = raw.get(name)
    if not isinstance(section, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in section.items():
        if isinstance(val, int) and not isinstance(val, bool):
            out[str(key)] = val
    return out


def _parse_severity_set(value: object, key: str, default: frozenset[str]) -> frozenset[str]:
    """Read one severity list from ``[gates]``, rejecting unknown or malformed entries."""
    if value is None:
        return default
    if not isinstance(value, list) or not value:
        raise ConfigInvalid(f"[gates] {key} must be a non-empty array of severities.")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item.strip().upper() not in SEVERITIES:
            allowed = ", ".join(sorted(SEVERITIES))
            raise ConfigInvalid(f"[gates] {key} has invalid severity {item!r}; allowed: {allowed}.")
        out.add(item.strip().upper())
    return frozenset(out)


def _parse_gates(raw: dict[str, object]) -> BlockingPolicy:
    """Build the gate blocking policy from ``[gates]``, defaulting to historic behavior."""
    section = raw.get("gates")
    if not isinstance(section, Mapping):
        return DEFAULT_BLOCKING_POLICY
    retry_mode = section.get("retry_blocking", DEFAULT_BLOCKING_POLICY.retry_mode)
    if not isinstance(retry_mode, str) or retry_mode.strip() not in RETRY_MODES:
        allowed = ", ".join(sorted(RETRY_MODES))
        raise ConfigInvalid(
            f"[gates] retry_blocking must be one of: {allowed} (got {retry_mode!r})."
        )
    initial = _parse_severity_set(
        section.get("blocking_severities"),
        "blocking_severities",
        DEFAULT_BLOCKING_POLICY.initial,
    )
    return BlockingPolicy(
        initial=initial,
        retry_mode=retry_mode.strip(),
        final=_parse_severity_set(section.get("final_round"), "final_round", initial),
    )


def _parse_phase(entry: object, index: int) -> PhaseDef:
    """Build a :class:`PhaseDef` from one ``[[phase]]`` table (shape-validated later)."""
    if not isinstance(entry, Mapping):
        raise ConfigInvalid(f"phase #{index} is not a table.")
    phase_id = entry.get("id")
    if not isinstance(phase_id, str) or not phase_id.strip():
        raise ConfigInvalid(f"phase #{index} is missing a non-empty 'id'.")
    phase_type = entry.get("type")
    if not isinstance(phase_type, str) or not phase_type.strip():
        raise ConfigInvalid(f"phase '{phase_id}' is missing a non-empty 'type'.")

    # `model = "x"` is shorthand for `models = ["x"]`.
    models = _as_str_tuple(entry.get("models")) or _as_str_tuple(entry.get("model"))

    retry = entry.get("retry_budget")
    retry_budget = retry if isinstance(retry, int) and not isinstance(retry, bool) else None

    def optional_positive_int(name: str) -> int | None:
        value = entry.get(name)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigInvalid(
                f"phase '{phase_id}' has invalid {name}; expected a positive integer."
            )
        return value

    persona = entry.get("persona")
    artifact = entry.get("artifact")
    step = entry.get("step")

    label = entry.get("label")
    parallel_group = entry.get("parallel_group")

    raw_audits = entry.get("audits")
    audits: list[AuditDef] = []
    if isinstance(raw_audits, list):
        for audit_index, raw_audit in enumerate(raw_audits):
            if not isinstance(raw_audit, Mapping):
                raise ConfigInvalid(f"phase '{phase_id}' audit #{audit_index} is not a table.")
            audit_id = raw_audit.get("id")
            audit_persona = raw_audit.get("persona")
            audit_model = raw_audit.get("model")
            if not isinstance(audit_id, str) or not audit_id.strip():
                raise ConfigInvalid(f"phase '{phase_id}' audit #{audit_index} is missing 'id'.")
            if not isinstance(audit_persona, str) or not audit_persona.strip():
                raise ConfigInvalid(f"phase '{phase_id}' audit '{audit_id}' is missing 'persona'.")
            if not isinstance(audit_model, str) or not audit_model.strip():
                raise ConfigInvalid(f"phase '{phase_id}' audit '{audit_id}' is missing 'model'.")
            audit_label = raw_audit.get("label")
            audits.append(
                AuditDef(
                    audit_id.strip(),
                    audit_label.strip()
                    if isinstance(audit_label, str) and audit_label.strip()
                    else audit_id.strip(),
                    audit_persona.strip(),
                    audit_model.strip(),
                    _as_str_tuple(raw_audit.get("skills")),
                )
            )

    return PhaseDef(
        id=phase_id.strip(),
        type=phase_type.strip(),
        label=label.strip() if isinstance(label, str) else "",
        persona=persona.strip() if isinstance(persona, str) and persona.strip() else None,
        skills=_as_str_tuple(entry.get("skills")),
        models=models,
        artifact=artifact.strip() if isinstance(artifact, str) and artifact.strip() else None,
        step=step.strip() if isinstance(step, str) and step.strip() else None,
        gates=bool(entry.get("gates", False)),
        structured_findings=entry.get("structured_findings") is True,
        retry_budget=retry_budget,
        max_artifact_chars=optional_positive_int("max_artifact_chars"),
        self_check=_self_check_enabled(entry.get("self_check")),
        self_check_persona=_self_check_persona(entry.get("self_check")),
        parallel_group=(
            parallel_group.strip()
            if isinstance(parallel_group, str) and parallel_group.strip()
            else None
        ),
        on_block=_as_str_tuple(entry.get("on_block")),
        selective_on_block=_as_str_tuple(entry.get("selective_on_block")),
        reconciles=_as_str_tuple(entry.get("reconciles")),
        against=_as_str_tuple(entry.get("against")),
        inputs=_as_str_tuple(entry.get("inputs")),
        synthesizes=_as_str_tuple(entry.get("synthesizes")),
        audits=tuple(audits),
        produces_contract=(
            str(entry.get("produces_contract", "")).strip()
            if isinstance(entry.get("produces_contract", ""), str)
            else ""
        ),
        accepts_contracts=_as_str_tuple(entry.get("accepts_contracts")),
        requires=_as_str_tuple(entry.get("requires")),
    )


def _parse_phases(raw: dict[str, object]) -> list[PhaseDef]:
    section = raw.get("phase")
    if not isinstance(section, list) or not section:
        raise ConfigInvalid("quillfolio.toml has no [[phase]] entries — at least one is required.")
    return [_parse_phase(entry, index) for index, entry in enumerate(section)]


def _parse_workflows(raw: dict[str, object]) -> tuple[dict[str, WorkflowDef], str]:
    """Parse named workflows, or normalize legacy root ``[[phase]]`` into ``ticket``."""
    section = raw.get("workflows")
    legacy = raw.get("phase")
    if section is None:
        phases = tuple(_parse_phases(raw))
        return {"ticket": WorkflowDef("ticket", "New ticket", "create", phases)}, "ticket"
    if legacy is not None:
        raise ConfigInvalid("quillfolio.toml cannot mix root [[phase]] with [workflows].")
    if not isinstance(section, Mapping):
        raise ConfigInvalid("[workflows] must be a table.")
    default = section.get("default")
    if not isinstance(default, str) or not default.strip():
        raise ConfigInvalid("[workflows].default must name a workflow.")
    workflows: dict[str, WorkflowDef] = {}
    for raw_id, value in section.items():
        if raw_id == "default":
            continue
        workflow_id = str(raw_id).strip()
        if not workflow_id or not isinstance(value, Mapping):
            raise ConfigInvalid(f"workflow '{raw_id}' must be a table.")
        phase_entries = value.get("phase")
        if not isinstance(phase_entries, list) or not phase_entries:
            raise ConfigInvalid(
                f"workflow '{workflow_id}' has no [[workflows.{workflow_id}.phase]] entries."
            )
        mode = value.get("mode", "create")
        if mode not in ("create", "update", "review"):
            raise ConfigInvalid(
                f"workflow '{workflow_id}' has invalid mode '{mode}' "
                "(expected create, update, or review)."
            )
        label = value.get("label")
        workflows[workflow_id] = WorkflowDef(
            id=workflow_id,
            label=label.strip() if isinstance(label, str) and label.strip() else workflow_id,
            mode=str(mode),
            phases=tuple(_parse_phase(entry, index) for index, entry in enumerate(phase_entries)),
            feedback_after_head=value.get("feedback_after_head") is True,
            resolve_review_threads=value.get("resolve_review_threads") is True,
        )
    default_id = default.strip()
    if default_id not in workflows:
        raise ConfigInvalid(f"default workflow '{default_id}' is not defined.")
    return workflows, default_id


# -- validation -------------------------------------------------------------------


def _validate_phases(config: QuillfolioConfig) -> None:
    """Validate the resolved phase set, failing fast with a specific message (#33 decision 3).

    Catches: duplicate ids, unknown type/step, an LLM phase with no persona/model, a finalizer
    with no ``reconciles`` (or one that names a phase with no reviewers), and any dangling
    ``on_block`` / ``reconciles`` target. Never let a config typo crash mid-run.
    """
    ids = config.phase_ids
    seen: set[str] = set()
    for pid in ids:
        if pid in seen:
            raise ConfigInvalid(f"duplicate phase id '{pid}'.")
        seen.add(pid)
    id_set = set(ids)
    parallel_groups: dict[str, list[PhaseDef]] = {}
    for phase in config.phases:
        if phase.parallel_group is not None:
            parallel_groups.setdefault(phase.parallel_group, []).append(phase)

    for group, members in parallel_groups.items():
        if len(members) < 2:
            raise ConfigInvalid(f"parallel_group '{group}' needs at least two producer phases.")
        if config.backend != "vllm":
            raise ConfigInvalid(f"parallel_group '{group}' requires runner backend = 'vllm'.")
        if any(member.type != "producer" for member in members):
            raise ConfigInvalid(f"parallel_group '{group}' may contain only producer phases.")
        if any(len(member.models) != 1 for member in members):
            raise ConfigInvalid(f"parallel_group '{group}' requires exactly one model per lane.")
        models = {member.model for member in members}
        if None in models or len(models) != 1:
            raise ConfigInvalid(f"parallel_group '{group}' must use one shared model.")
        indexes = [ids.index(member.id) for member in members]
        if indexes != list(range(min(indexes), max(indexes) + 1)):
            raise ConfigInvalid(f"parallel_group '{group}' phases must be consecutive.")

    for ph in config.phases:
        if ph.type not in PHASE_TYPES:
            raise ConfigInvalid(
                f"phase '{ph.id}' has unknown type '{ph.type}' "
                f"(expected one of {', '.join(PHASE_TYPES)})."
            )

        if ph.self_check and config.runner != "pi":
            raise ConfigInvalid(
                f"phase '{ph.id}' enables self_check, which requires runner.kind = 'pi'."
            )

        if (
            ph.self_check_persona is not None
            and not config.persona_path(ph.self_check_persona).is_file()
        ):
            raise ConfigInvalid(
                f"phase '{ph.id}' names self_check persona '{ph.self_check_persona}', "
                "which does not exist in the persona library."
            )

        if ph.type == "mechanical":
            if ph.self_check:
                raise ConfigInvalid(f"mechanical phase '{ph.id}' cannot enable self_check.")
            if ph.step not in MECHANICAL_STEPS:
                raise ConfigInvalid(
                    f"mechanical phase '{ph.id}' has unknown step '{ph.step}' "
                    f"(expected one of {', '.join(MECHANICAL_STEPS)})."
                )
        elif not ph.audits:
            # producer / reviewer / finalizer are all LLM phases (branch + commit are producers now).
            _require_persona_model(ph, config)

        if ph.parallel_group is not None and ph.audits:
            raise ConfigInvalid(
                f"phase '{ph.id}' cannot combine parallel_group with reviewer audits."
            )

        if ph.audits:
            if ph.type != "reviewer":
                raise ConfigInvalid(f"phase '{ph.id}' defines audits but is not a reviewer.")
            if ph.gates:
                raise ConfigInvalid(
                    f"concurrent audit phase '{ph.id}' cannot gate directly; use a finalizer."
                )
            if ph.persona is not None or ph.models:
                raise ConfigInvalid(
                    f"concurrent audit phase '{ph.id}' must use per-audit persona/model only."
                )
            if config.backend != "vllm":
                raise ConfigInvalid(
                    f"concurrent audit phase '{ph.id}' requires runner backend = 'vllm'."
                )
            if len(ph.audits) < 2:
                raise ConfigInvalid(f"concurrent audit phase '{ph.id}' needs at least two audits.")
            audit_ids = [audit.id for audit in ph.audits]
            if len(set(audit_ids)) != len(audit_ids):
                raise ConfigInvalid(f"concurrent audit phase '{ph.id}' has duplicate audit ids.")
            models = {audit.model for audit in ph.audits}
            if len(models) != 1:
                raise ConfigInvalid(f"concurrent audit phase '{ph.id}' must use one shared model.")
            for audit in ph.audits:
                if not config.persona_path(audit.persona).is_file():
                    raise ConfigInvalid(
                        f"phase '{ph.id}' audit '{audit.id}' names missing persona "
                        f"'{audit.persona}' ({config.personas_root})."
                    )

        if ph.type == "finalizer":
            if not ph.reconciles:
                raise ConfigInvalid(
                    f"finalizer phase '{ph.id}' must list the phases it reconciles "
                    "(reconciles = [...])."
                )
            for target in ph.reconciles:
                tgt = config.phase(target)
                if tgt is None:
                    raise ConfigInvalid(f"finalizer '{ph.id}' reconciles unknown phase '{target}'.")
                if tgt.type != "reviewer":
                    raise ConfigInvalid(
                        f"finalizer '{ph.id}' reconciles '{target}', which is not a reviewer."
                    )

        # A reviewer/finalizer may name the producer artifacts it reviews against; the engine
        # injects those filenames into the prompt so the judge never guesses (or hardcodes) them.
        # The target must run BEFORE this phase, else its artifact won't exist at review time.
        for target in ph.against:
            tgt = config.phase(target)
            if tgt is None:
                raise ConfigInvalid(f"phase '{ph.id}' reviews against unknown phase '{target}'.")
            if not tgt.artifact:
                raise ConfigInvalid(
                    f"phase '{ph.id}' reviews against '{target}', which produces no artifact."
                )
            if ids.index(target) >= ids.index(ph.id):
                raise ConfigInvalid(
                    f"phase '{ph.id}' reviews against '{target}', which does not run before it."
                )

        if ph.inputs and ph.type != "producer":
            raise ConfigInvalid(f"phase '{ph.id}' defines inputs but is not a producer.")
        if ph.synthesizes and ph.type != "producer":
            raise ConfigInvalid(f"phase '{ph.id}' defines synthesizes but is not a producer.")
        if len(set(ph.synthesizes)) != len(ph.synthesizes):
            raise ConfigInvalid(f"phase '{ph.id}' synthesizes duplicate phase ids.")
        for target in (*ph.inputs, *ph.synthesizes):
            tgt = config.phase(target)
            if tgt is None:
                raise ConfigInvalid(f"phase '{ph.id}' reads unknown input phase '{target}'.")
            if not tgt.artifact:
                raise ConfigInvalid(
                    f"phase '{ph.id}' reads input '{target}', which produces no artifact."
                )
            if ids.index(target) >= ids.index(ph.id):
                raise ConfigInvalid(
                    f"phase '{ph.id}' reads input '{target}', which does not run before it."
                )
            if ph.parallel_group is not None and tgt.parallel_group == ph.parallel_group:
                raise ConfigInvalid(
                    f"parallel phase '{ph.id}' cannot read sibling lane '{target}'."
                )

        if ph.gates:
            if len(ph.on_block) > 1:
                raise ConfigInvalid(
                    f"phase '{ph.id}' has multiple on_block targets; on_block is a back-edge "
                    "to one earlier phase, after which normal phase traversal resumes."
                )
            for target in ph.on_block:
                if target not in id_set:
                    raise ConfigInvalid(
                        f"phase '{ph.id}' has on_block = '{target}', which is not a phase id."
                    )
                if ids.index(target) >= ids.index(ph.id):
                    raise ConfigInvalid(
                        f"phase '{ph.id}' has on_block = '{target}', which does not run before it."
                    )
                target_phase = config.phase(target)
                if (
                    target_phase is not None
                    and target_phase.parallel_group is not None
                    and not ph.selective_on_block
                ):
                    raise ConfigInvalid(
                        f"phase '{ph.id}' cannot use a parallel lane as a plain on_block target; "
                        "use selective_on_block with a serial synthesis phase."
                    )
        if ph.selective_on_block:
            if not ph.gates or not ph.structured_findings:
                raise ConfigInvalid(
                    f"phase '{ph.id}' selective_on_block requires a structured gate."
                )
            if len(ph.on_block) > 1:
                raise ConfigInvalid(
                    f"phase '{ph.id}' selective_on_block allows no on_block in direct mode or one "
                    "synthesis on_block phase."
                )
            candidates: list[PhaseDef] = []
            if len(set(ph.selective_on_block)) != len(ph.selective_on_block):
                raise ConfigInvalid(f"phase '{ph.id}' selective_on_block has duplicate phase ids.")
            for target in ph.selective_on_block:
                candidate = config.phase(target)
                if candidate is None:
                    raise ConfigInvalid(
                        f"phase '{ph.id}' selectively retries unknown phase '{target}'."
                    )
                if candidate.type != "producer" or candidate.parallel_group is None:
                    raise ConfigInvalid(
                        f"phase '{ph.id}' selective target '{target}' is not a parallel producer."
                    )
                boundary = ids.index(ph.on_block[0]) if ph.on_block else ids.index(ph.id)
                if ids.index(target) >= boundary:
                    raise ConfigInvalid(
                        f"phase '{ph.id}' selective target '{target}' must run before its retry "
                        "boundary."
                    )
                candidates.append(candidate)
            groups = {candidate.parallel_group for candidate in candidates}
            if len(groups) != 1:
                raise ConfigInvalid(
                    f"phase '{ph.id}' selective_on_block targets must share one parallel_group."
                )
            if ph.on_block:
                synthesis = config.phase(ph.on_block[0])
                assert synthesis is not None
                if synthesis.type != "producer" or synthesis.parallel_group is not None:
                    raise ConfigInvalid(
                        f"phase '{ph.id}' on_block target '{synthesis.id}' must be a serial "
                        "producer synthesis."
                    )
                if set(synthesis.synthesizes) != set(ph.selective_on_block):
                    raise ConfigInvalid(
                        f"phase '{ph.id}' synthesis '{synthesis.id}' must synthesize every "
                        "selective retry lane exactly once."
                    )
            else:
                missing_against = sorted(set(ph.selective_on_block) - set(ph.against))
                if missing_against:
                    raise ConfigInvalid(
                        f"phase '{ph.id}' direct selective gate must review every retry lane; "
                        f"missing against: {', '.join(missing_against)}."
                    )
            selected_group = candidates[0].parallel_group
            assert selected_group is not None
            all_group_ids = {
                candidate.id
                for candidate in config.phases
                if candidate.parallel_group == selected_group
            }
            if set(ph.selective_on_block) != all_group_ids:
                raise ConfigInvalid(
                    f"phase '{ph.id}' selective_on_block must include every lane in parallel_group "
                    f"'{selected_group}'."
                )
        if ph.structured_findings and ph.type not in ("reviewer", "finalizer"):
            raise ConfigInvalid(
                f"phase '{ph.id}' enables structured_findings but is not a reviewer/finalizer."
            )

    for phase in config.phases:
        _validate_phase_contract(config, phase)
    _validate_contract_edges(config)


def phase_contract_dependencies(config: QuillfolioConfig) -> dict[str, tuple[str, ...]]:
    """Return every forward contract dependency keyed by consumer phase ID.

    Retry contracts flow in the opposite direction and are validated separately because their
    producer gate appears later in the normal phase order.
    """
    dependencies: dict[str, tuple[str, ...]] = {}
    for phase in config.phases:
        ordered = (*phase.inputs, *phase.synthesizes, *phase.against, *phase.reconciles, *phase.requires)
        dependencies[phase.id] = tuple(dict.fromkeys(ordered))
    return dependencies


def _validate_phase_contract(config: QuillfolioConfig, phase: PhaseDef) -> None:
    if not phase.produces_contract:
        raise ConfigInvalid(
            f"phase '{phase.id}' is missing produces_contract; every phase must publish an exact "
            "versioned handoff (for example 'quill.artifact/v1')."
        )
    try:
        spec = default_catalog().resolve(phase.produces_contract)
    except ContractError as exc:
        raise ConfigInvalid(f"phase '{phase.id}' has invalid produces_contract: {exc}") from exc
    if phase.type not in spec.phase_types:
        raise ConfigInvalid(
            f"phase '{phase.id}' type '{phase.type}' cannot produce {phase.produces_contract}."
        )
    if spec.steps and phase.step not in spec.steps:
        allowed = ", ".join(spec.steps)
        raise ConfigInvalid(
            f"phase '{phase.id}' step '{phase.step}' cannot produce {phase.produces_contract}; "
            f"allowed steps: {allowed}."
        )
    if phase.type == "mechanical" and not spec.steps:
        raise ConfigInvalid(
            f"mechanical phase '{phase.id}' uses {phase.produces_contract}, which is not bound to "
            "a mechanical step."
        )
    if len(set(phase.accepts_contracts)) != len(phase.accepts_contracts):
        raise ConfigInvalid(f"phase '{phase.id}' accepts_contracts contains duplicates.")
    for identifier in phase.accepts_contracts:
        try:
            default_catalog().resolve(identifier)
        except ContractError as exc:
            raise ConfigInvalid(
                f"phase '{phase.id}' has invalid accepts_contracts entry: {exc}"
            ) from exc
    if len(set(phase.requires)) != len(phase.requires):
        raise ConfigInvalid(f"phase '{phase.id}' requires contains duplicate phase ids.")


def _validate_contract_edges(config: QuillfolioConfig) -> None:
    ids = config.phase_ids
    dependencies = phase_contract_dependencies(config)
    for phase in config.phases:
        semantic = (*phase.inputs, *phase.synthesizes, *phase.against, *phase.reconciles)
        overlap = sorted(set(semantic) & set(phase.requires))
        if overlap:
            raise ConfigInvalid(
                f"phase '{phase.id}' repeats semantic dependencies in requires: {', '.join(overlap)}."
            )
        seen_relations: dict[str, str] = {}
        for relation, targets in (
            ("inputs", phase.inputs),
            ("synthesizes", phase.synthesizes),
            ("against", phase.against),
            ("reconciles", phase.reconciles),
        ):
            for target in targets:
                previous = seen_relations.get(target)
                if previous is not None:
                    raise ConfigInvalid(
                        f"phase '{phase.id}' repeats dependency '{target}' across "
                        f"{previous} and {relation}."
                    )
                seen_relations[target] = relation
        for target in phase.requires:
            if target not in ids:
                raise ConfigInvalid(f"phase '{phase.id}' requires unknown phase '{target}'.")
            if ids.index(target) >= ids.index(phase.id):
                raise ConfigInvalid(
                    f"phase '{phase.id}' requires '{target}', which does not run before it."
                )

    incoming: dict[str, list[str]] = {phase.id: list(dependencies[phase.id]) for phase in config.phases}
    # A blocking gate's validated result is consumed by every revise target. Selective lanes are the
    # actual consumers even in synthesis-backed mode; the serial synthesis also consumes the gate
    # through on_block and the selected lane contracts through synthesizes.
    retry_producers: dict[str, list[str]] = {phase.id: [] for phase in config.phases}
    for gate in config.phases:
        if not gate.gates:
            continue
        targets = tuple(dict.fromkeys((*gate.selective_on_block, *gate.on_block)))
        for target in targets:
            if target in retry_producers:
                retry_producers[target].append(gate.id)

    for phase in config.phases:
        producers = incoming[phase.id] + retry_producers[phase.id]
        expected = {
            producer.produces_contract
            for producer_id in producers
            if (producer := config.phase(producer_id)) is not None
        }
        accepted = set(phase.accepts_contracts)
        missing = sorted(expected - accepted)
        unused = sorted(accepted - expected)
        if missing:
            raise ConfigInvalid(
                f"phase '{phase.id}' does not accept required contract(s): {', '.join(missing)}."
            )
        if unused:
            raise ConfigInvalid(
                f"phase '{phase.id}' accepts contract(s) with no declared edge: {', '.join(unused)}."
            )


def _self_check_enabled(value: object) -> bool:
    """``true`` or a persona filename both enable the check; anything else disables it."""
    if value is True:
        return True
    return isinstance(value, str) and bool(value.strip())


def _self_check_persona(value: object) -> str | None:
    """The persona filename when ``self_check`` names one, else ``None`` for the generic prompt."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _require_persona_model(ph: PhaseDef, config: QuillfolioConfig) -> None:
    """An LLM phase needs a persona file that exists in the library, and at least one model."""
    if ph.persona is None:
        raise ConfigInvalid(f"phase '{ph.id}' (type {ph.type}) is missing 'persona'.")
    if not config.persona_path(ph.persona).is_file():
        raise ConfigInvalid(
            f"phase '{ph.id}' names persona '{ph.persona}', which is not in the persona library "
            f"({config.personas_root}). Check `GET /personas` for what is available."
        )
    if not ph.models:
        raise ConfigInvalid(f"phase '{ph.id}' (type {ph.type}) is missing 'model' / 'models'.")


# -- resolution -------------------------------------------------------------------


def _resolve(
    directory: Path,
    raw: dict[str, object],
    *,
    personas_root: Path,
    runs_root: Path,
    vllm_url: str,
) -> QuillfolioConfig:
    repo_section = raw.get("repo") if isinstance(raw.get("repo"), dict) else {}
    build_section = raw.get("build") if isinstance(raw.get("build"), dict) else {}
    runner_section = raw.get("runner") if isinstance(raw.get("runner"), dict) else {}
    assert isinstance(repo_section, dict)
    assert isinstance(build_section, dict)
    assert isinstance(runner_section, dict)

    repo = repo_section.get("name")
    if not isinstance(repo, str) or not repo.strip():
        repo = _detect_repo(directory) or ""
    repo = repo.strip()

    pr_base = repo_section.get("pr_base")
    if not isinstance(pr_base, str) or not pr_base.strip():
        pr_base = _detect_default_branch(directory)

    project_board = repo_section.get("project_board")
    if not isinstance(project_board, str) or not project_board.strip():
        project_board = None

    excluded_issue_labels = tuple(
        dict.fromkeys(
            label.casefold() for label in _as_str_tuple(repo_section.get("excluded_issue_labels"))
        )
    )

    runner = runner_section.get("kind")
    runner = runner.strip() if isinstance(runner, str) else ""

    backend = runner_section.get("backend")
    backend = backend.strip() if isinstance(backend, str) and backend.strip() else DEFAULT_BACKEND

    vllm_section = runner_section.get("vllm")
    vllm_section = vllm_section if isinstance(vllm_section, dict) else {}
    vllm_command = _as_str_tuple(vllm_section.get("command"))
    raw_vllm_models = vllm_section.get("models")
    vllm_models = (
        {
            str(model).strip(): service.strip()
            for model, service in raw_vllm_models.items()
            if str(model).strip() and isinstance(service, str) and service.strip()
        }
        if isinstance(raw_vllm_models, dict)
        else {}
    )

    build_command = build_section.get("command")
    test_command = build_section.get("test")
    build_command = build_command.strip() if isinstance(build_command, str) else ""
    test_command = test_command.strip() if isinstance(test_command, str) else ""

    log_dir = build_section.get("log_dir")
    log_dir = log_dir if isinstance(log_dir, str) and log_dir.strip() else DEFAULT_LOG_DIR

    timeouts = _parse_int_section(raw, "timeouts")
    memory_section = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    assert isinstance(memory_section, dict)

    workflows, default_workflow = _parse_workflows(raw)
    return QuillfolioConfig(
        directory=directory,
        repo=repo,
        pr_base=pr_base.strip(),
        runner=runner,
        build_command=build_command,
        test_command=test_command,
        log_dir=log_dir,
        phases=list(workflows[default_workflow].phases),
        workflows=workflows,
        workflow_id=default_workflow,
        backend=backend,
        vllm_url=vllm_url,
        vllm_command=vllm_command,
        vllm_models=vllm_models,
        project_board=project_board,
        excluded_issue_labels=excluded_issue_labels,
        retries=_parse_int_section(raw, "retries"),
        gates=_parse_gates(raw),
        opencode_run_seconds=timeouts.get("opencode_run_seconds", DEFAULT_OPENCODE_RUN_SECONDS),
        model_load_seconds=timeouts.get("model_load_seconds", DEFAULT_MODEL_LOAD_SECONDS),
        ci_seconds=timeouts.get("ci_seconds", DEFAULT_CI_SECONDS),
        memory_enabled=memory_section.get("enabled") is True,
        personas_root=personas_root,
        runs_root=runs_root,
        memory_root=runs_root.parent / "memory",
    )


# -- entry point ------------------------------------------------------------------


def config_path(directory: str | Path) -> Path:
    """``<directory>/quillfolio.toml``."""
    return Path(directory) / CONFIG_FILENAME


def load_config_text(
    text: str,
    *,
    directory: str | Path,
    personas_root: Path | None = None,
    runs_root: Path | None = None,
    vllm_url: str | None = None,
    source: str = CONFIG_FILENAME,
) -> QuillfolioConfig:
    """Resolve TOML ``text`` into a validated config.

    ``directory`` is the checkout used for git detection (``repo``, ``pr_base``) when the text
    omits those fields.

    ``source`` only labels error messages.

    Raises:
        ConfigInvalid: malformed TOML, blank build/test, no phases, a phase typo, an unknown
            persona, a dangling ``on_block`` / ``reconciles`` target, ...
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalid(f"{source}: not valid TOML ({exc}).") from exc

    runner_section = raw.get("runner") if isinstance(raw.get("runner"), dict) else {}
    assert isinstance(runner_section, dict)
    if "server_url" in runner_section:
        raise ConfigInvalid(
            f"{source}: runner.server_url is not allowed in repository config; "
            "set QUILL_VLLM_URL in the machine environment instead."
        )

    resolved_vllm_url = (
        vllm_url if vllm_url is not None else os.environ.get("QUILL_VLLM_URL", "")
    ).strip()

    config = _resolve(
        Path(directory),
        raw,
        personas_root=personas_root or default_personas_root(),
        runs_root=runs_root or default_runs_root(),
        vllm_url=resolved_vllm_url,
    )

    missing = [
        name
        for name, value in (
            ("runner.kind", config.runner),
            ("build.command", config.build_command),
            ("build.test", config.test_command),
        )
        if not value
    ]
    if missing:
        raise ConfigInvalid(
            f"{source}: {' and '.join(missing)} must be set before a run. "
            "Fill them in, then re-run."
        )

    if config.backend not in BACKENDS:
        raise ConfigInvalid(
            f"{source}: runner.backend '{config.backend}' is unknown "
            f"(expected one of {', '.join(BACKENDS)})."
        )
    if config.backend == "vllm" and not config.vllm_url:
        raise ConfigInvalid(
            f'{source}: runner.backend = "vllm" requires QUILL_VLLM_URL in the machine '
            'environment (e.g. "http://vllm.example:8000").'
        )
    if config.vllm_models and not config.vllm_command:
        raise ConfigInvalid(
            f"{source}: runner.vllm.models requires a non-empty runner.vllm.command array."
        )

    for workflow_id in config.workflows:
        _validate_phases(config.select_workflow(workflow_id))
    return config


def load_config(
    directory: str | Path,
    *,
    personas_root: Path | None = None,
    runs_root: Path | None = None,
    vllm_url: str | None = None,
) -> QuillfolioConfig:
    """Resolve ``<directory>/quillfolio.toml`` into a validated config.

    A thin wrapper over :func:`load_config_text` so the file path and the upload path share one
    validator — a config that passes here passes there, and vice versa.

    Raises:
        ConfigMissing: no ``quillfolio.toml`` — the user must run ``quill --init``.
        ConfigInvalid: the config is present but malformed.
    """
    directory = Path(directory)
    path = config_path(directory)
    if not path.is_file():
        raise ConfigMissing(directory)

    return load_config_text(
        path.read_text(encoding="utf-8"),
        directory=directory,
        personas_root=personas_root,
        runs_root=runs_root,
        vllm_url=vllm_url,
        source=str(path),
    )


# Slug helper used by the engine for per-model fan-out findings filenames (#33 decision 5).
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, replace non-alphanumeric runs with ``-``, strip edges. Filesystem-safe."""
    return _SLUG_RE.sub("-", value.lower()).strip("-")
