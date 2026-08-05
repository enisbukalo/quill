"""Versioned, hash-bound phase contracts for durable Quill handoffs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import quote


CONTRACT_ENVELOPE = "quill.phase-contract"
CONTRACT_ENVELOPE_VERSION = 1
CONTRACT_SPECS_DIR = Path(__file__).parent / "_contract_specs"
_CONTRACT_ID = re.compile(r"^(?P<kind>[a-z][a-z0-9_.-]*)/v(?P<version>[1-9][0-9]*)$")
_SAFE_PHASE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "min_length",
        "min_items",
        "additional_properties",
    }
)


class ContractError(ValueError):
    """A contract specification, payload, envelope, or bound artifact is invalid."""


class ContractStatus(StrEnum):
    """Materialization state, independent of a phase's PASS/BLOCK verdict."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """One validated data-driven payload specification."""

    kind: str
    version: int
    phase_types: tuple[str, ...]
    steps: tuple[str, ...]
    allowed_statuses: frozenset[ContractStatus]
    payload_schema: dict[str, object]
    semantic_obligations: tuple[str, ...]
    digest: str

    @property
    def identifier(self) -> str:
        return f"{self.kind}/v{self.version}"

    def validate_payload(self, payload: object) -> None:
        """Validate ``payload`` against this spec's deliberately small schema vocabulary."""
        _validate_value(payload, self.payload_schema, path="payload")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Hash-bound immutable evidence referenced by a phase contract."""

    path: str
    snapshot: str
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class UpstreamContractRef:
    """Exact upstream attempt consumed by a phase."""

    phase_id: str
    kind: str
    version: int
    attempt: int
    path: str
    digest: str


@dataclass(frozen=True, slots=True)
class ContractRef:
    """Small validated pointer retained in ``PhaseResult`` and ``RunContext``."""

    phase_id: str
    kind: str
    version: int
    status: ContractStatus
    attempt: int
    path: str
    digest: str


@dataclass(frozen=True, slots=True)
class PhaseContract:
    """Validated durable envelope for one phase attempt."""

    kind: str
    version: int
    contract_status: ContractStatus
    phase_outcome: str
    run_id: str
    workflow: str
    phase_id: str
    phase_type: str
    attempt: int
    created_at: str
    spec_digest: str
    source_artifacts: tuple[ArtifactRef, ...]
    git_head: str | None
    checkpoint: str | None
    worktree_fingerprint: str | None
    upstream: tuple[UpstreamContractRef, ...]
    payload: object
    digest: str = ""

    def unsigned_dict(self) -> dict[str, object]:
        """Return the canonical envelope fields covered by :attr:`digest`."""
        return {
            "envelope": CONTRACT_ENVELOPE,
            "envelope_version": CONTRACT_ENVELOPE_VERSION,
            "kind": self.kind,
            "version": self.version,
            "contract_status": self.contract_status.value,
            "phase_outcome": self.phase_outcome,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "phase_id": self.phase_id,
            "phase_type": self.phase_type,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "spec_digest": self.spec_digest,
            "source_artifacts": [
                {
                    "path": item.path,
                    "snapshot": item.snapshot,
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                }
                for item in self.source_artifacts
            ],
            "repository": {
                "git_head": self.git_head,
                "checkpoint": self.checkpoint,
                "worktree_fingerprint": self.worktree_fingerprint,
            },
            "upstream": [
                {
                    "phase_id": item.phase_id,
                    "kind": item.kind,
                    "version": item.version,
                    "attempt": item.attempt,
                    "path": item.path,
                    "digest": item.digest,
                }
                for item in self.upstream
            ],
            "payload": self.payload,
        }

    def with_digest(self) -> PhaseContract:
        """Return an equivalent envelope with a recomputed canonical digest."""
        return PhaseContract(
            kind=self.kind,
            version=self.version,
            contract_status=self.contract_status,
            phase_outcome=self.phase_outcome,
            run_id=self.run_id,
            workflow=self.workflow,
            phase_id=self.phase_id,
            phase_type=self.phase_type,
            attempt=self.attempt,
            created_at=self.created_at,
            spec_digest=self.spec_digest,
            source_artifacts=self.source_artifacts,
            git_head=self.git_head,
            checkpoint=self.checkpoint,
            worktree_fingerprint=self.worktree_fingerprint,
            upstream=self.upstream,
            payload=self.payload,
            digest=canonical_digest(self.unsigned_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        """Return the complete JSON object, computing its digest when needed."""
        completed = self if self.digest else self.with_digest()
        return {**completed.unsigned_dict(), "digest": completed.digest}

    def ref(self, path: str) -> ContractRef:
        """Create a lightweight pointer to this validated envelope."""
        completed = self if self.digest else self.with_digest()
        return ContractRef(
            phase_id=completed.phase_id,
            kind=completed.kind,
            version=completed.version,
            status=completed.contract_status,
            attempt=completed.attempt,
            path=path,
            digest=completed.digest,
        )


class ContractCatalog:
    """Strict loader and resolver for packaged contract specifications."""

    def __init__(self, root: Path = CONTRACT_SPECS_DIR) -> None:
        self.root = root
        self._specs = self._load()

    def _load(self) -> dict[str, ContractSpec]:
        if not self.root.is_dir():
            raise ContractError(f"contract specification directory is missing: {self.root}")
        specs: dict[str, ContractSpec] = {}
        paths = sorted(self.root.glob("*.json"))
        if not paths:
            raise ContractError(f"contract specification directory is empty: {self.root}")
        for path in paths:
            try:
                raw = strict_json_loads(path.read_text(encoding="utf-8"))
            except (OSError, ContractError) as exc:
                raise ContractError(f"invalid contract specification {path.name}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ContractError(f"contract specification {path.name} must be one object")
            spec = _parse_spec(cast(dict[str, object], raw), path.name)
            if spec.identifier in specs:
                raise ContractError(f"duplicate contract specification {spec.identifier}")
            specs[spec.identifier] = spec
        return specs

    def resolve(self, identifier: str) -> ContractSpec:
        """Return the exact known contract version or raise an actionable error."""
        parse_contract_id(identifier)
        try:
            return self._specs[identifier]
        except KeyError as exc:
            raise ContractError(f"unknown contract specification '{identifier}'") from exc

    @property
    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


@cache
def default_catalog() -> ContractCatalog:
    """Return the immutable packaged catalog, loaded once per process."""
    return ContractCatalog()


def parse_contract_id(identifier: str) -> tuple[str, int]:
    """Parse ``kind/vN`` without accepting whitespace, zero, signs, or ambiguous versions."""
    match = _CONTRACT_ID.fullmatch(identifier)
    if match is None:
        raise ContractError(
            f"invalid contract identifier {identifier!r}; expected lowercase 'kind/vN'"
        )
    return match.group("kind"), int(match.group("version"))


def safe_phase_id(phase_id: str) -> str:
    """Encode a phase ID as one reversible, path-safe component."""
    if not phase_id or phase_id in {".", ".."}:
        raise ContractError("phase id cannot be empty, '.' or '..'")
    if _SAFE_PHASE_ID.fullmatch(phase_id):
        return phase_id
    encoded = quote(phase_id, safe="._-")
    if not encoded or encoded in {".", ".."} or "/" in encoded or "\\" in encoded:
        raise ContractError(f"phase id cannot be encoded safely: {phase_id!r}")
    return encoded


def canonical_json(value: object) -> str:
    """Serialize JSON deterministically for hashing and durable identity."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"value is not canonical JSON: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def strict_json_loads(text: str) -> object:
    """Decode standards-compliant JSON while rejecting duplicate object keys and NaN/Infinity."""

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> object:
        raise ContractError(f"invalid non-finite JSON number {value}")

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


def file_sha256(path: Path) -> str:
    """Hash one regular file without loading it all into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"could not hash artifact {path}: {exc}") from exc
    return digest.hexdigest()


def snapshot_artifact(
    run_dir: Path,
    path: Path,
    phase_id: str,
    attempt: int,
    *,
    slot: int | None = None,
) -> ArtifactRef:
    """Copy and hash an artifact into an immutable per-attempt evidence location."""
    if attempt <= 0:
        raise ContractError("contract attempt must be positive")
    source = _jailed_file(run_dir, path)
    relative = source.relative_to(run_dir.resolve())
    slot_suffix = f"-{slot}" if slot is not None else ""
    snapshot_rel = (
        Path("work")
        / safe_phase_id(phase_id)
        / f"attempt-{attempt}{slot_suffix}{source.suffix}"
    )
    snapshot = prepare_output_path(run_dir, run_dir / snapshot_rel)
    if snapshot.exists():
        raise ContractError(f"immutable artifact snapshot already exists: {snapshot_rel}")
    try:
        data = source.read_bytes()
        snapshot.write_bytes(data)
    except OSError as exc:
        raise ContractError(f"could not snapshot artifact {relative}: {exc}") from exc
    return ArtifactRef(
        path=relative.as_posix(),
        snapshot=snapshot_rel.as_posix(),
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
    )


def verify_artifact_ref(run_dir: Path, artifact: ArtifactRef) -> None:
    """Verify both the mutable source path and immutable snapshot still match their binding."""
    if artifact.bytes < 0 or not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256):
        raise ContractError("artifact reference has invalid size or SHA-256")
    for name in (artifact.path, artifact.snapshot):
        target = _jailed_file(run_dir, run_dir / name)
        if target.stat().st_size != artifact.bytes:
            raise ContractError(f"artifact size mismatch: {name}")
        if file_sha256(target) != artifact.sha256:
            raise ContractError(f"artifact hash mismatch: {name}")


def verify_artifact_snapshot(run_dir: Path, artifact: ArtifactRef) -> None:
    """Verify the immutable evidence copy without requiring the mutable latest-view path.

    Historical contract attempts remain valid after a later attempt updates the canonical readable
    artifact.  Projection-time mutation checks still use :func:`verify_artifact_ref` to compare
    both locations while an attempt is active.
    """
    if artifact.bytes < 0 or not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256):
        raise ContractError("artifact reference has invalid size or SHA-256")
    target = _jailed_file(run_dir, run_dir / artifact.snapshot)
    if target.stat().st_size != artifact.bytes:
        raise ContractError(f"artifact size mismatch: {artifact.snapshot}")
    if file_sha256(target) != artifact.sha256:
        raise ContractError(f"artifact hash mismatch: {artifact.snapshot}")


def repository_identity(directory: Path) -> tuple[str | None, str | None]:
    """Return Git HEAD and a deterministic dirty/untracked fingerprint when available."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=directory,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=directory,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    digest = hashlib.sha256(diff)
    for raw_name in sorted(name for name in untracked.split(b"\0") if name):
        digest.update(raw_name)
        try:
            relative = Path(os.fsdecode(raw_name))
            target = directory / relative
            if target.is_file() and not target.is_symlink():
                digest.update(bytes.fromhex(file_sha256(target)))
        except (OSError, UnicodeError, ContractError):
            digest.update(b"<unreadable>")
    return head or None, digest.hexdigest()


def new_contract(
    *,
    spec: ContractSpec,
    status: ContractStatus,
    phase_outcome: str,
    run_id: str,
    workflow: str,
    phase_id: str,
    phase_type: str,
    attempt: int,
    source_artifacts: tuple[ArtifactRef, ...],
    upstream: tuple[UpstreamContractRef, ...],
    payload: object,
    git_head: str | None = None,
    checkpoint: str | None = None,
    worktree_fingerprint: str | None = None,
) -> PhaseContract:
    """Build and validate a new envelope without publishing it."""
    if status not in spec.allowed_statuses:
        raise ContractError(f"{spec.identifier} does not allow status {status.value}")
    if phase_type not in spec.phase_types:
        raise ContractError(f"{spec.identifier} cannot be produced by phase type {phase_type!r}")
    if attempt <= 0:
        raise ContractError("contract attempt must be positive")
    _validate_contract_payload(spec, status, payload)
    return PhaseContract(
        kind=spec.kind,
        version=spec.version,
        contract_status=status,
        phase_outcome=phase_outcome,
        run_id=run_id,
        workflow=workflow,
        phase_id=phase_id,
        phase_type=phase_type,
        attempt=attempt,
        created_at=datetime.now(UTC).isoformat(),
        spec_digest=spec.digest,
        source_artifacts=source_artifacts,
        git_head=git_head,
        checkpoint=checkpoint,
        worktree_fingerprint=worktree_fingerprint,
        upstream=upstream,
        payload=payload,
    ).with_digest()


def publish_contract(run_dir: Path, contract: PhaseContract, catalog: ContractCatalog) -> ContractRef:
    """Validate and atomically publish an immutable attempt plus its ``latest`` pointer."""
    validated = validate_contract(contract, catalog)
    for artifact in validated.source_artifacts:
        verify_artifact_ref(run_dir, artifact)
    phase_rel = Path("contracts") / safe_phase_id(validated.phase_id)
    attempt_path = prepare_output_path(
        run_dir,
        run_dir / phase_rel / f"attempt-{validated.attempt}.json",
    )
    latest_path = prepare_output_path(run_dir, run_dir / phase_rel / "latest.json")
    if attempt_path.exists():
        raise ContractError(
            f"immutable contract attempt already exists: "
            f"{phase_rel.joinpath(attempt_path.name).as_posix()}"
        )
    encoded = json.dumps(validated.as_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    _atomic_write(attempt_path, encoded, replace=False)
    _atomic_write(latest_path, encoded, replace=True)
    relative = phase_rel.joinpath(attempt_path.name).as_posix()
    return validated.ref(relative)


def publish_compatibility_view(run_dir: Path, source: Path, target: Path) -> None:
    """Atomically copy validated staged JSON to a legacy artifact path.

    Compatibility views are deliberately downstream of validation and never authoritative.  Both
    paths are jailed to the run directory and symlink targets are rejected so a model-controlled
    filename cannot turn publication into an arbitrary write.
    """
    staged = _jailed_file(run_dir, source)
    destination = prepare_output_path(run_dir, target)
    try:
        text = staged.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"could not read validated compatibility view: {exc}") from exc
    _atomic_write(destination, text.rstrip("\n") + "\n", replace=True)


def load_contract(
    path: Path,
    catalog: ContractCatalog,
    *,
    run_dir: Path | None = None,
    verify_artifacts: bool = True,
) -> PhaseContract:
    """Load and fully validate a contract envelope and its optional bound artifacts."""
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ContractError) as exc:
        raise ContractError(f"invalid contract JSON at {path}: {exc}") from exc
    contract = _contract_from_raw(raw)
    validated = validate_contract(contract, catalog)
    if run_dir is not None and verify_artifacts:
        for artifact in validated.source_artifacts:
            verify_artifact_snapshot(run_dir, artifact)
    return validated


def validate_contract(contract: PhaseContract, catalog: ContractCatalog) -> PhaseContract:
    """Validate envelope invariants, spec compatibility, payload, references, and digest."""
    spec = catalog.resolve(f"{contract.kind}/v{contract.version}")
    if contract.spec_digest != spec.digest:
        raise ContractError(f"contract spec digest mismatch for {spec.identifier}")
    if contract.contract_status not in spec.allowed_statuses:
        raise ContractError(
            f"{spec.identifier} does not allow status {contract.contract_status.value}"
        )
    if contract.phase_type not in spec.phase_types:
        raise ContractError(
            f"{spec.identifier} cannot be produced by phase type {contract.phase_type!r}"
        )
    if contract.attempt <= 0:
        raise ContractError("contract attempt must be positive")
    if not contract.run_id or not contract.workflow or not contract.phase_id or not contract.phase_outcome:
        raise ContractError("contract identity fields must be non-empty")
    safe_phase_id(contract.phase_id)
    try:
        created_at = datetime.fromisoformat(contract.created_at)
    except ValueError as exc:
        raise ContractError("contract created_at must be ISO-8601") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ContractError("contract created_at must include a timezone offset")
    for name, value in (("git_head", contract.git_head), ("checkpoint", contract.checkpoint)):
        if value is not None and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
            raise ContractError(f"contract repository {name} must be a full Git object ID")
    if contract.worktree_fingerprint is not None and re.fullmatch(
        r"[0-9a-f]{64}", contract.worktree_fingerprint
    ) is None:
        raise ContractError("contract worktree_fingerprint must be SHA-256")
    _validate_contract_payload(spec, contract.contract_status, contract.payload)
    artifact_paths: set[str] = set()
    artifact_snapshots: set[str] = set()
    expected_snapshot_parent = PurePosixPath("work") / safe_phase_id(contract.phase_id)
    expected_snapshot_name = re.compile(
        rf"^attempt-{contract.attempt}(?:-[0-9]+)?(?:\.[^/]+)?$"
    )
    for artifact in contract.source_artifacts:
        source_path = _validated_relative_path(artifact.path, "artifact source")
        snapshot_path = _validated_relative_path(artifact.snapshot, "artifact snapshot")
        if artifact.path in artifact_paths or artifact.snapshot in artifact_snapshots:
            raise ContractError("contract contains duplicate artifact source or snapshot paths")
        artifact_paths.add(artifact.path)
        artifact_snapshots.add(artifact.snapshot)
        if (
            snapshot_path.parent != expected_snapshot_parent
            or expected_snapshot_name.fullmatch(snapshot_path.name) is None
        ):
            raise ContractError(
                f"artifact snapshot path does not match phase attempt: {artifact.snapshot}"
            )
        if source_path == snapshot_path:
            raise ContractError("artifact source and immutable snapshot paths must differ")
    seen: set[str] = set()
    for item in contract.upstream:
        if not item.phase_id:
            raise ContractError("upstream phase id must be non-empty")
        if item.phase_id in seen:
            raise ContractError(f"duplicate upstream phase reference {item.phase_id!r}")
        seen.add(item.phase_id)
        if item.attempt <= 0 or not re.fullmatch(r"[0-9a-f]{64}", item.digest):
            raise ContractError(f"invalid upstream reference for {item.phase_id!r}")
        parse_contract_id(f"{item.kind}/v{item.version}")
        expected_path = (
            PurePosixPath("contracts")
            / safe_phase_id(item.phase_id)
            / f"attempt-{item.attempt}.json"
        )
        if _validated_relative_path(item.path, "upstream contract") != expected_path:
            raise ContractError(f"invalid upstream path for {item.phase_id!r}")
    expected = canonical_digest(contract.unsigned_dict())
    if not contract.digest or contract.digest != expected:
        raise ContractError("contract digest mismatch")
    return contract


def upstream_ref(ref: ContractRef) -> UpstreamContractRef:
    return UpstreamContractRef(
        phase_id=ref.phase_id,
        kind=ref.kind,
        version=ref.version,
        attempt=ref.attempt,
        path=ref.path,
        digest=ref.digest,
    )


def _parse_spec(raw: dict[str, object], filename: str) -> ContractSpec:
    allowed = {
        "kind",
        "version",
        "phase_types",
        "steps",
        "allowed_statuses",
        "payload",
        "semantic_obligations",
    }
    extras = sorted(set(raw) - allowed)
    if extras:
        raise ContractError(f"contract specification {filename} has unknown keys: {', '.join(extras)}")
    kind = raw.get("kind")
    version = raw.get("version")
    if not isinstance(kind, str) or not isinstance(version, int) or isinstance(version, bool):
        raise ContractError(f"contract specification {filename} has invalid kind/version")
    parse_contract_id(f"{kind}/v{version}")
    phase_types = _string_tuple(raw.get("phase_types"), f"{filename}.phase_types")
    if not phase_types:
        raise ContractError(f"contract specification {filename} has no phase types")
    if not set(phase_types) <= {"producer", "reviewer", "finalizer", "mechanical"}:
        raise ContractError(f"contract specification {filename} has an unknown phase type")
    steps = _string_tuple(raw.get("steps", []), f"{filename}.steps")
    statuses = _string_tuple(raw.get("allowed_statuses"), f"{filename}.allowed_statuses")
    try:
        allowed_statuses = frozenset(ContractStatus(item) for item in statuses)
    except ValueError as exc:
        raise ContractError(f"contract specification {filename} has an invalid status") from exc
    if not allowed_statuses:
        raise ContractError(f"contract specification {filename} has no allowed statuses")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise ContractError(f"contract specification {filename} payload must be an object schema")
    schema = cast(dict[str, object], payload)
    _validate_schema(schema, path=f"{filename}.payload")
    obligations = _string_tuple(
        raw.get("semantic_obligations", []), f"{filename}.semantic_obligations"
    )
    digest = canonical_digest(raw)
    return ContractSpec(
        kind=kind,
        version=version,
        phase_types=phase_types,
        steps=steps,
        allowed_statuses=allowed_statuses,
        payload_schema=schema,
        semantic_obligations=obligations,
        digest=digest,
    )


def _validate_schema(schema: dict[str, object], *, path: str) -> None:
    extras = sorted(set(schema) - _SCHEMA_KEYS)
    if extras:
        raise ContractError(f"{path} has unknown schema keys: {', '.join(extras)}")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in _SCHEMA_TYPES:
        raise ContractError(f"{path}.type must be one supported type")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ContractError(f"{path}.enum must be a non-empty array")
    for key in ("min_length", "min_items"):
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ContractError(f"{path}.{key} must be a non-negative integer")
    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise ContractError(f"{path}.properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, dict):
                raise ContractError(f"{path}.properties entries must be object schemas")
            _validate_schema(cast(dict[str, object], child), path=f"{path}.{name}")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ContractError(f"{path}.required must be an array of property names")
        if len(set(required)) != len(required) or not set(required) <= set(properties):
            raise ContractError(f"{path}.required contains duplicate or unknown properties")
        additional = schema.get("additional_properties", False)
        if not isinstance(additional, bool):
            raise ContractError(f"{path}.additional_properties must be boolean")
    elif kind == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise ContractError(f"{path}.items must be a schema")
        _validate_schema(cast(dict[str, object], items), path=f"{path}[]")


def _validate_value(value: object, schema: dict[str, object], *, path: str) -> None:
    kind = cast(str, schema["type"])
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[kind]
    if not valid:
        raise ContractError(f"{path} must be {kind}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ContractError(f"{path} must be one of {enum!r}")
    if kind == "string":
        minimum = cast(int, schema.get("min_length", 0))
        if len(cast(str, value)) < minimum:
            raise ContractError(f"{path} must contain at least {minimum} characters")
    elif kind == "array":
        rows = cast(list[object], value)
        minimum = cast(int, schema.get("min_items", 0))
        if len(rows) < minimum:
            raise ContractError(f"{path} must contain at least {minimum} items")
        item_schema = cast(dict[str, object], schema["items"])
        for index, item in enumerate(rows):
            _validate_value(item, item_schema, path=f"{path}[{index}]")
    elif kind == "object":
        mapping = cast(dict[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise ContractError(f"{path} keys must be strings")
        properties = cast(dict[str, dict[str, object]], schema["properties"])
        required = cast(list[str], schema.get("required", []))
        missing = [name for name in required if name not in mapping]
        if missing:
            raise ContractError(f"{path} is missing required fields: {', '.join(missing)}")
        if schema.get("additional_properties", False) is False:
            extras = sorted(set(cast(dict[str, object], mapping)) - set(properties))
            if extras:
                raise ContractError(f"{path} has unknown fields: {', '.join(extras)}")
        for name, child in properties.items():
            if name in mapping:
                _validate_value(mapping[name], child, path=f"{path}.{name}")


def _contract_from_raw(raw: object) -> PhaseContract:
    if not isinstance(raw, dict):
        raise ContractError("contract must contain one JSON object")
    mapping = cast(dict[str, object], raw)
    expected = {
        "envelope",
        "envelope_version",
        "kind",
        "version",
        "contract_status",
        "phase_outcome",
        "run_id",
        "workflow",
        "phase_id",
        "phase_type",
        "attempt",
        "created_at",
        "spec_digest",
        "source_artifacts",
        "repository",
        "upstream",
        "payload",
        "digest",
    }
    if set(mapping) != expected:
        missing = sorted(expected - set(mapping))
        extra = sorted(set(mapping) - expected)
        raise ContractError(f"contract envelope fields mismatch; missing={missing}, extra={extra}")
    if mapping["envelope"] != CONTRACT_ENVELOPE or mapping["envelope_version"] != 1:
        raise ContractError("unsupported contract envelope")
    source_raw = mapping["source_artifacts"]
    upstream_raw = mapping["upstream"]
    repository = mapping["repository"]
    if not isinstance(source_raw, list) or not isinstance(upstream_raw, list):
        raise ContractError("contract source_artifacts/upstream must be arrays")
    if not isinstance(repository, dict) or set(repository) != {
        "git_head",
        "checkpoint",
        "worktree_fingerprint",
    }:
        raise ContractError("contract repository identity has invalid fields")
    for item in source_raw:
        if not isinstance(item, dict) or set(item) != {"path", "snapshot", "sha256", "bytes"}:
            raise ContractError("contract artifact reference has invalid fields")
    for item in upstream_raw:
        if not isinstance(item, dict) or set(item) != {
            "phase_id",
            "kind",
            "version",
            "attempt",
            "path",
            "digest",
        }:
            raise ContractError("contract upstream reference has invalid fields")
    try:
        sources = tuple(
            ArtifactRef(
                path=_required_str(item, "path"),
                snapshot=_required_str(item, "snapshot"),
                sha256=_required_str(item, "sha256"),
                bytes=_required_int(item, "bytes", non_negative=True),
            )
            for item in source_raw
        )
        upstream = tuple(
            UpstreamContractRef(
                phase_id=_required_str(item, "phase_id"),
                kind=_required_str(item, "kind"),
                version=_required_int(item, "version"),
                attempt=_required_int(item, "attempt"),
                path=_required_str(item, "path"),
                digest=_required_str(item, "digest"),
            )
            for item in upstream_raw
        )
        return PhaseContract(
            kind=_required_str(mapping, "kind"),
            version=_required_int(mapping, "version"),
            contract_status=ContractStatus(_required_str(mapping, "contract_status")),
            phase_outcome=_required_str(mapping, "phase_outcome"),
            run_id=_required_str(mapping, "run_id"),
            workflow=_required_str(mapping, "workflow"),
            phase_id=_required_str(mapping, "phase_id"),
            phase_type=_required_str(mapping, "phase_type"),
            attempt=_required_int(mapping, "attempt"),
            created_at=_required_str(mapping, "created_at"),
            spec_digest=_required_str(mapping, "spec_digest"),
            source_artifacts=sources,
            git_head=_optional_str(repository, "git_head"),
            checkpoint=_optional_str(repository, "checkpoint"),
            worktree_fingerprint=_optional_str(repository, "worktree_fingerprint"),
            upstream=upstream,
            payload=mapping["payload"],
            digest=_required_str(mapping, "digest"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"contract envelope has invalid field types: {exc}") from exc


def _required_str(mapping: object, key: str) -> str:
    if not isinstance(mapping, dict):
        raise ContractError(f"contract field {key!r} has no object")
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"contract field {key!r} must be a non-empty string")
    return value


def _optional_str(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContractError(f"contract field {key!r} must be null or non-empty string")
    return value


def _required_int(mapping: object, key: str, *, non_negative: bool = False) -> int:
    if not isinstance(mapping, dict):
        raise ContractError(f"contract field {key!r} has no object")
    value = mapping.get(key)
    lower = 0 if non_negative else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < lower:
        raise ContractError(f"contract field {key!r} must be an integer >= {lower}")
    return value


def _string_tuple(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{path} must be an array of non-empty strings")
    result = tuple(cast(list[str], value))
    if len(set(result)) != len(result):
        raise ContractError(f"{path} must not contain duplicates")
    return result


def _validated_relative_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ContractError(f"{label} path is not a normalized relative path: {value!r}")
    return path


def _jailed_file(run_dir: Path, path: Path) -> Path:
    root = run_dir.resolve()
    try:
        target = path.resolve()
    except OSError as exc:
        raise ContractError(f"artifact path cannot be resolved: {path}: {exc}") from exc
    if target == root or root not in target.parents:
        raise ContractError(f"artifact path escapes run directory: {path}")
    if not target.is_file() or target.is_symlink():
        raise ContractError(f"artifact is not a regular non-symlink file: {path}")
    return target


def prepare_output_path(run_dir: Path, path: Path) -> Path:
    """Create a jailed output parent without following any symlinked path component.

    Contract and staging paths may be influenced by phase IDs or files left by a failed model
    attempt.  Resolving only the final path is insufficient because ``mkdir(exist_ok=True)`` follows
    a pre-existing directory symlink.  Walk each lexical component beneath the run directory and
    reject symlinks before returning the resolved destination.
    """
    try:
        lexical_root = Path(os.path.abspath(run_dir))
        requested = path if path.is_absolute() else run_dir / path
        lexical_target = Path(os.path.abspath(requested))
        relative = lexical_target.relative_to(lexical_root)
        root = run_dir.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ContractError(f"output path escapes or cannot resolve run directory: {path}") from exc
    if not root.is_dir() or root.is_symlink():
        raise ContractError(f"run directory is not a regular directory: {run_dir}")
    if relative == Path(".") or not relative.name:
        raise ContractError(f"output path must name a file beneath the run directory: {path}")

    parent = root
    for component in relative.parts[:-1]:
        if component in {"", ".", ".."}:
            raise ContractError(f"output path has an unsafe component: {path}")
        parent /= component
        try:
            parent.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ContractError(f"could not create output directory {parent}: {exc}") from exc
        if parent.is_symlink() or not parent.is_dir():
            raise ContractError(f"output path uses a non-directory or symlink component: {parent}")

    destination = parent / relative.name
    if destination.is_symlink():
        raise ContractError(f"output path cannot replace a symlink: {destination}")
    if destination.exists() and not destination.is_file():
        raise ContractError(f"output path is not a regular file: {destination}")
    return destination


def _atomic_write(path: Path, text: str, *, replace: bool) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise ContractError(f"stale contract temporary file exists: {temporary}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ContractError(f"refusing to overwrite immutable contract: {path}") from exc
            temporary.unlink()
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    except OSError as exc:
        raise ContractError(f"could not publish contract {path}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _validate_contract_payload(
    spec: ContractSpec, status: ContractStatus, payload: object
) -> None:
    """Validate complete payloads against their spec and incomplete payloads against one shape."""
    if status is not ContractStatus.INCOMPLETE:
        spec.validate_payload(payload)
        return
    if not isinstance(payload, dict) or set(payload) != {"missing"}:
        raise ContractError("incomplete payload must contain only a non-empty 'missing' array")
    missing = payload.get("missing")
    if not isinstance(missing, list) or not missing:
        raise ContractError("incomplete payload missing must be a non-empty array")
    seen: set[str] = set()
    for index, row in enumerate(missing, 1):
        if not isinstance(row, dict) or set(row) != {"field", "reason", "evidence"}:
            raise ContractError(f"incomplete payload item #{index} has invalid fields")
        for key in ("field", "reason", "evidence"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"incomplete payload item #{index} has invalid {key}")
        field = cast(str, row["field"]).strip()
        if field in seen:
            raise ContractError(f"incomplete payload repeats field {field!r}")
        seen.add(field)
