"""Durable lineage carried from a terminal run into a phase restart."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from quill import events
from quill.config import QuillfolioConfig, phase_contract_dependencies, slugify
from quill.contracts import (
    ContractError,
    ContractRef,
    ContractStatus,
    PhaseContract,
    default_catalog,
    file_sha256,
    load_contract,
    prepare_output_path,
    safe_phase_id,
)

SEED_NAME = "restart-lineage.json"


class RestartError(ValueError):
    """A restart seed or inherited contract closure is absent, stale, or unsafe."""


def model_overrides(run_dir: Path, executions: list[dict[str, Any]]) -> dict[str, str]:
    """Return the effective source-run model choices, preferring observed execution evidence."""
    inherited: dict[str, str] = {}
    path = run_dir / "state.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                raw = event.get("model_overrides")
                if event.get("type") == events.RUN_QUEUED and isinstance(raw, dict):
                    inherited.update(
                        {
                            str(phase): str(model)
                            for phase, model in raw.items()
                            if isinstance(phase, str) and isinstance(model, str)
                        }
                    )

    observed: dict[str, set[str]] = {}
    for execution in executions:
        phase = execution.get("phase")
        model = execution.get("model")
        if not isinstance(phase, str) or not isinstance(model, str) or "+" in model:
            continue
        configured_phase = phase.split(".", 1)[0]
        observed.setdefault(configured_phase, set()).add(model)
    for phase, models in observed.items():
        if len(models) == 1:
            inherited[phase] = next(iter(models))
    return inherited


def seed_events(
    source_run_id: str,
    source_dir: Path,
    executions: list[dict[str, Any]],
) -> list[events.Event]:
    """Build a compact replay of completed source work for the new run's graph and history."""
    result: list[events.Event] = []
    plan = _latest_plan(source_dir / "state.jsonl")
    if plan is not None:
        result.append(_inherited(plan, source_run_id))

    cutoff = max(
        (
            float(value)
            for execution in executions
            if isinstance((value := execution.get("finished_at")), (int, float))
            and not isinstance(value, bool)
        ),
        default=None,
    )
    if cutoff is not None:
        result.extend(_model_load_events(source_dir / "state.jsonl", cutoff, source_run_id))

    for index, execution in enumerate(executions, 1):
        phase = str(execution["phase"])
        label = str(execution.get("label") or phase)
        finished = _number(execution.get("finished_at"), float(index))
        duration = _optional_number(execution.get("duration_s"))
        started = _number(
            execution.get("started_at"),
            finished - duration if duration is not None else finished,
        )
        started_event: events.Event = {
            "type": events.PHASE_STARTED,
            "ts": started,
            "phase": phase,
            "label": label,
            "attempt": execution.get("call_number") or 1,
            "max_attempts": execution.get("call_number") or 1,
            "phase_type": execution.get("phase_type"),
            "model": execution.get("model"),
        }
        result.append(_inherited(started_event, source_run_id))
        self_check = execution.get("self_check_status")
        if self_check in {"active", "passed", "failed"}:
            result.append(
                _inherited(
                    {
                        "type": events.SELF_CHECK_STARTED,
                        "ts": started,
                        "phase": phase,
                        "label": label,
                    },
                    source_run_id,
                )
            )
            if self_check != "active":
                result.append(
                    _inherited(
                        {
                            "type": events.SELF_CHECK_DONE,
                            "ts": finished,
                            "phase": phase,
                            "label": label,
                            "verdict": "PASS" if self_check == "passed" else "BLOCK",
                            "duration_s": execution.get("self_check_duration_s") or 0.0,
                        },
                        source_run_id,
                    )
                )
        self_fix = execution.get("self_fix_status")
        if self_fix in {"active", "completed", "failed"}:
            result.append(
                _inherited(
                    {
                        "type": events.SELF_FIX_STARTED,
                        "ts": started,
                        "phase": phase,
                        "label": label,
                    },
                    source_run_id,
                )
            )
            if self_fix != "active":
                result.append(
                    _inherited(
                        {
                            "type": events.SELF_FIX_DONE,
                            "ts": finished,
                            "phase": phase,
                            "label": label,
                            "repaired": self_fix == "completed",
                            "duration_s": execution.get("self_fix_duration_s") or 0.0,
                        },
                        source_run_id,
                    )
                )
        verdict = execution.get("verdict")
        terminal_type = events.GATE_VERDICT if verdict in {"PASS", "BLOCK"} else events.PHASE_DONE
        result.append(
            _inherited(
                {
                    "type": terminal_type,
                    "ts": finished,
                    "phase": phase,
                    "label": label,
                    "verdict": verdict,
                    "model": execution.get("model"),
                    "duration_s": duration,
                    "tools": execution.get("tool_calls_by_name"),
                    "reason": execution.get("rejection_reason"),
                    "contract_kind": execution.get("contract_kind"),
                    "contract_version": execution.get("contract_version"),
                    "contract_status": execution.get("contract_status"),
                    "contract_digest": execution.get("contract_digest"),
                },
                source_run_id,
            )
        )
    return result


def write_seed(
    target_dir: Path,
    *,
    source_run_id: str,
    source_sequence: int,
    phase: str,
    start_phase: str,
    executions: list[dict[str, Any]],
    phase_set_hash: str,
    checkpoint: str,
) -> None:
    """Persist the exact transcript subset and selection used by artifact inheritance."""
    transcripts = sorted(
        {
            str(name)
            for execution in executions
            for name in execution.get("transcripts", [])
            if isinstance(name, str) and name.startswith("stream-")
        }
    )
    payload = {
        "version": 2,
        "source_run_id": source_run_id,
        "source_sequence": source_sequence,
        "phase": phase,
        "start_phase": start_phase,
        "phase_set_hash": phase_set_hash,
        "checkpoint": checkpoint,
        "transcripts": transcripts,
        "contracts": [],
        "artifacts": [],
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / SEED_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def seed_transcripts(target_dir: Path) -> set[str]:
    """Read the allowlisted source transcripts for a prepared restart."""
    try:
        raw = json.loads((target_dir / SEED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    names = raw.get("transcripts") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        return set()
    return {
        name
        for name in names
        if isinstance(name, str) and name.startswith("stream-") and "/" not in name
    }


def source_phase_set_hash(source_dir: Path) -> str | None:
    """Return the last durable phase-set fingerprint emitted by the source run."""
    plan = _latest_plan(source_dir / "state.jsonl")
    value = plan.get("phase_set_hash") if plan is not None else None
    return value if isinstance(value, str) and value else None


def prepare_contract_restart(
    source_dir: Path,
    target_dir: Path,
    *,
    config: QuillfolioConfig,
    start_phase: str,
    source_run_id: str,
    checkpoint: str,
) -> dict[str, ContractRef]:
    """Validate and copy the exact transitive contract/evidence closure for ``start_phase``.

    All source data is validated before a staging directory is copied into the target. The source
    run's mutable logs, findings, compatibility files, and unrelated contracts are never inherited.
    """
    seed = _load_seed(target_dir)
    _validate_seed_identity(
        seed,
        config=config,
        start_phase=start_phase,
        source_run_id=source_run_id,
        checkpoint=checkpoint,
    )
    contracts = _contract_closure(
        source_dir,
        config=config,
        start_phase=start_phase,
        source_run_id=source_run_id,
    )
    artifacts: dict[str, str] = {}
    contract_paths: dict[str, PhaseContract] = {}
    for path, contract in contracts.items():
        contract_paths[path] = contract
        for artifact in contract.source_artifacts:
            relative = artifact.snapshot
            observed = _safe_source_file(source_dir, relative)
            digest = file_sha256(observed)
            if digest != artifact.sha256:
                raise RestartError(
                    f"restart artifact hash mismatch for {relative}: expected "
                    f"{artifact.sha256}, got {digest}"
                )
            previous = artifacts.setdefault(relative, digest)
            if previous != digest:
                raise RestartError(f"restart closure has conflicting artifact {relative}")

    transcripts = seed_transcripts(target_dir)
    for name in transcripts:
        _safe_source_file(source_dir, name)

    target_dir.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".restart-stage-", dir=target_dir))
    try:
        for relative in sorted((*contract_paths, *artifacts, *transcripts)):
            source = _safe_source_file(source_dir, relative)
            destination = stage_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for staged_path in sorted(stage_root.rglob("*")):
            if not staged_path.is_file():
                continue
            staged_relative = staged_path.relative_to(stage_root)
            destination = prepare_output_path(target_dir, target_dir / staged_relative)
            if destination.exists() or destination.is_symlink():
                raise RestartError(f"restart target already contains inherited path {staged_relative}")
            staged_path.replace(destination)
    except (OSError, ContractError) as exc:
        raise RestartError(f"could not stage restart contract closure: {exc}") from exc
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    entries = [_contract_seed_entry(path, contract) for path, contract in sorted(contract_paths.items())]
    artifact_entries = [
        {"path": path, "sha256": digest} for path, digest in sorted(artifacts.items())
    ]
    completed_seed = {
        **seed,
        "contracts": entries,
        "artifacts": artifact_entries,
    }
    _write_seed_payload(target_dir, completed_seed)
    return _refs_from_seed(target_dir, completed_seed, config=config, start_phase=start_phase)


def restart_contract_refs(
    target_dir: Path, *, config: QuillfolioConfig, start_phase: str
) -> dict[str, ContractRef]:
    """Revalidate a prepared seed and restore its exact inherited refs into a new run context."""
    seed = _load_seed(target_dir)
    if seed.get("phase_set_hash") != config.phase_set_hash():
        raise RestartError("restart phase-set hash no longer matches the selected workflow")
    if seed.get("start_phase") != start_phase:
        raise RestartError("restart seed start phase does not match the requested phase")
    return _refs_from_seed(target_dir, seed, config=config, start_phase=start_phase)


def _contract_closure(
    source_dir: Path,
    *,
    config: QuillfolioConfig,
    start_phase: str,
    source_run_id: str,
) -> dict[str, PhaseContract]:
    phase = config.phase(start_phase)
    if phase is None:
        raise RestartError(f"restart phase {start_phase!r} is not configured")
    graph = phase_contract_dependencies(config)
    direct_dependencies = graph.get(start_phase, ())
    direct_ids = [
        contract_id
        for dependency in direct_dependencies
        for contract_id in _configured_contract_ids(config, dependency)
    ]
    result: dict[str, PhaseContract] = {}
    visiting: set[str] = set()

    def visit(relative: str, *, expected_digest: str | None = None) -> PhaseContract:
        if relative in result:
            contract = result[relative]
            if expected_digest is not None and contract.digest != expected_digest:
                raise RestartError(f"restart upstream digest mismatch for {relative}")
            return contract
        if relative in visiting:
            raise RestartError(f"restart contract upstream cycle at {relative}")
        visiting.add(relative)
        path = _safe_source_file(source_dir, relative)
        try:
            contract = load_contract(path, default_catalog(), run_dir=source_dir)
        except ContractError as exc:
            raise RestartError(f"invalid restart contract {relative}: {exc}") from exc
        if contract.contract_status is not ContractStatus.COMPLETE:
            raise RestartError(
                f"restart requires COMPLETE contract for {contract.phase_id}, got "
                f"{contract.contract_status.value}"
            )
        if contract.run_id != source_run_id or contract.workflow != config.workflow_id:
            raise RestartError(f"restart contract source identity mismatch for {relative}")
        if expected_digest is not None and contract.digest != expected_digest:
            raise RestartError(f"restart upstream digest mismatch for {relative}")
        for upstream in contract.upstream:
            inherited = visit(upstream.path, expected_digest=upstream.digest)
            if (
                inherited.phase_id != upstream.phase_id
                or inherited.kind != upstream.kind
                or inherited.version != upstream.version
                or inherited.attempt != upstream.attempt
            ):
                raise RestartError(f"restart upstream identity mismatch for {upstream.phase_id}")
        visiting.remove(relative)
        result[relative] = contract
        return contract

    for contract_id in direct_ids:
        latest = Path("contracts") / safe_phase_id(contract_id) / "latest.json"
        latest_path = _safe_source_file(source_dir, latest.as_posix())
        try:
            latest_contract = load_contract(latest_path, default_catalog(), run_dir=source_dir)
        except ContractError as exc:
            raise RestartError(f"invalid latest restart contract for {contract_id}: {exc}") from exc
        if latest_contract.phase_id != contract_id:
            raise RestartError(f"latest restart contract identity mismatch for {contract_id}")
        if latest_contract.run_id != source_run_id or latest_contract.workflow != config.workflow_id:
            raise RestartError(f"latest restart contract source identity mismatch for {contract_id}")
        identifier = f"{latest_contract.kind}/v{latest_contract.version}"
        if identifier not in phase.accepts_contracts:
            raise RestartError(
                f"restart phase {start_phase!r} cannot consume {identifier} from {contract_id!r}"
            )
        attempt_path = (
            Path("contracts")
            / safe_phase_id(contract_id)
            / f"attempt-{latest_contract.attempt}.json"
        ).as_posix()
        exact = visit(attempt_path, expected_digest=latest_contract.digest)
        if exact.as_dict() != latest_contract.as_dict():
            raise RestartError(f"latest restart pointer does not match exact attempt for {contract_id}")
    return result


def _configured_contract_ids(config: QuillfolioConfig, phase_id: str) -> tuple[str, ...]:
    phase = config.phase(phase_id)
    if phase is None:
        raise RestartError(f"restart dependency {phase_id!r} is not configured")
    if phase.audits:
        return tuple(f"{phase.id}.{audit.id}" for audit in phase.audits)
    if phase.is_fanout:
        return tuple(f"{phase.id}.{slugify(model)}" for model in phase.models)
    return (phase.id,)


def _safe_source_file(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise RestartError(f"unsafe restart path {relative!r}")
    source_root = root.resolve()
    try:
        resolved = (root / candidate).resolve()
    except OSError as exc:
        raise RestartError(f"restart path cannot be resolved {relative!r}: {exc}") from exc
    if source_root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
        raise RestartError(f"restart path is not a safe regular file: {relative!r}")
    return resolved


def _load_seed(target_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((target_dir / SEED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RestartError(f"restart lineage seed is missing or malformed: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 2:
        raise RestartError("restart lineage seed has an unsupported version")
    return raw


def _validate_seed_identity(
    seed: dict[str, Any],
    *,
    config: QuillfolioConfig,
    start_phase: str,
    source_run_id: str,
    checkpoint: str,
) -> None:
    expected = {
        "source_run_id": source_run_id,
        "start_phase": start_phase,
        "phase_set_hash": config.phase_set_hash(),
        "checkpoint": checkpoint,
    }
    for key, value in expected.items():
        if seed.get(key) != value:
            raise RestartError(f"restart lineage {key} does not match the requested boundary")
    if not isinstance(seed.get("source_sequence"), int) or isinstance(
        seed.get("source_sequence"), bool
    ):
        raise RestartError("restart lineage source sequence is invalid")


def _contract_seed_entry(relative: str, contract: PhaseContract) -> dict[str, object]:
    return {
        "phase_id": contract.phase_id,
        "kind": contract.kind,
        "version": contract.version,
        "attempt": contract.attempt,
        "path": relative,
        "digest": contract.digest,
        "status": contract.contract_status.value,
    }


def _refs_from_seed(
    target_dir: Path,
    seed: dict[str, Any],
    *,
    config: QuillfolioConfig,
    start_phase: str,
) -> dict[str, ContractRef]:
    raw_contracts = seed.get("contracts")
    raw_artifacts = seed.get("artifacts")
    if not isinstance(raw_contracts, list) or not isinstance(raw_artifacts, list):
        raise RestartError("restart lineage contract closure is missing")
    expected_artifacts: dict[str, str] = {}
    for row in raw_artifacts:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise RestartError("restart lineage artifact entry is malformed")
        path, digest = row.get("path"), row.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise RestartError("restart lineage artifact entry has invalid types")
        if path in expected_artifacts:
            raise RestartError(f"restart lineage repeats artifact {path}")
        expected_artifacts[path] = digest
        if file_sha256(_safe_source_file(target_dir, path)) != digest:
            raise RestartError(f"restart inherited artifact hash mismatch for {path}")
    refs: dict[str, ContractRef] = {}
    loaded_contracts: dict[str, PhaseContract] = {}
    seen_paths: set[str] = set()
    for row in raw_contracts:
        if not isinstance(row, dict) or set(row) != {
            "phase_id", "kind", "version", "attempt", "path", "digest", "status"
        }:
            raise RestartError("restart lineage contract entry is malformed")
        relative = row.get("path")
        if not isinstance(relative, str) or relative in seen_paths:
            raise RestartError("restart lineage contract path is invalid or repeated")
        seen_paths.add(relative)
        try:
            contract = load_contract(
                _safe_source_file(target_dir, relative),
                default_catalog(),
                run_dir=target_dir,
            )
        except ContractError as exc:
            raise RestartError(f"invalid inherited restart contract {relative}: {exc}") from exc
        expected = _contract_seed_entry(relative, contract)
        if row != expected:
            raise RestartError(f"restart lineage entry does not match contract {relative}")
        if contract.phase_id in refs:
            raise RestartError(f"restart lineage repeats phase contract {contract.phase_id}")
        refs[contract.phase_id] = contract.ref(relative)
        loaded_contracts[contract.phase_id] = contract

    bound_artifacts = {
        artifact.snapshot: artifact.sha256
        for contract in loaded_contracts.values()
        for artifact in contract.source_artifacts
    }
    if bound_artifacts != expected_artifacts:
        raise RestartError("restart lineage artifact inventory does not match contract bindings")

    # Recompute the closure from the copied files by temporarily supplying latest pointers is
    # unnecessary: every exact upstream is checked below and every direct dependency must exist.
    for contract in loaded_contracts.values():
        for upstream in contract.upstream:
            inherited = refs.get(upstream.phase_id)
            if inherited is None or inherited.digest != upstream.digest or inherited.path != upstream.path:
                raise RestartError(f"restart closure is missing upstream {upstream.phase_id}")
    phase = config.phase(start_phase)
    assert phase is not None
    direct_ids = {
        contract_id
        for dependency in phase_contract_dependencies(config).get(start_phase, ())
        for contract_id in _configured_contract_ids(config, dependency)
    }
    missing = sorted(direct_ids - set(refs))
    if missing:
        raise RestartError("restart closure is missing direct contract(s): " + ", ".join(missing))
    for contract_id in direct_ids:
        ref = refs[contract_id]
        if f"{ref.kind}/v{ref.version}" not in phase.accepts_contracts:
            raise RestartError(f"restart closure has incompatible contract from {contract_id}")
    reachable: set[str] = set()

    def mark(phase_id: str) -> None:
        if phase_id in reachable:
            return
        reachable.add(phase_id)
        for upstream in loaded_contracts[phase_id].upstream:
            mark(upstream.phase_id)

    for contract_id in direct_ids:
        mark(contract_id)
    if reachable != set(refs):
        extras = sorted(set(refs) - reachable)
        raise RestartError("restart closure contains unrelated contract(s): " + ", ".join(extras))
    return refs


def _write_seed_payload(target_dir: Path, payload: dict[str, Any]) -> None:
    temporary = target_dir / f".{SEED_NAME}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise RestartError("stale restart lineage temporary file exists")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target_dir / SEED_NAME)
    except OSError as exc:
        raise RestartError(f"could not persist restart lineage: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _latest_plan(path: Path) -> events.Event | None:
    latest: events.Event | None = None
    if not path.is_file():
        return None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == events.RUN_PLAN:
                latest = event
    return latest


def _model_load_events(path: Path, cutoff: float, source_run_id: str) -> list[events.Event]:
    result: list[events.Event] = []
    if not path.is_file():
        return result
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") not in {
                events.MODEL_LOADING,
                events.MODEL_LOAD_DONE,
            }:
                continue
            timestamp = event.get("ts")
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                if float(timestamp) <= cutoff:
                    result.append(_inherited(event, source_run_id))
    return result


def _inherited(event: events.Event, source_run_id: str) -> events.Event:
    copied = {key: value for key, value in event.items() if value is not None}
    copied["inherited_from"] = source_run_id
    return copied


def _number(value: object, default: float) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _optional_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
