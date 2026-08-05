"""Adversarial coverage for durable phase contracts."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from quill.contracts import (
    CONTRACT_SPECS_DIR,
    ArtifactRef,
    ContractCatalog,
    ContractError,
    ContractStatus,
    UpstreamContractRef,
    canonical_digest,
    canonical_json,
    load_contract,
    new_contract,
    parse_contract_id,
    publish_compatibility_view,
    publish_contract,
    safe_phase_id,
    snapshot_artifact,
    verify_artifact_ref,
)


def _payload() -> dict[str, object]:
    return {
        "summary": "Grounded research",
        "requirements": ["R1"],
        "evidence": ["ticket:R1"],
        "unknowns": [],
        "obligations": ["Preserve R1"],
    }


def _contract(tmp_path: Path, *, phase_id: str = "research_requirements"):
    artifact = tmp_path / "research.md"
    artifact.write_text("# Research\n", encoding="utf-8")
    evidence = snapshot_artifact(tmp_path, artifact, phase_id, 1)
    catalog = ContractCatalog()
    spec = catalog.resolve("quill.research.requirements/v1")
    contract = new_contract(
        spec=spec,
        status=ContractStatus.COMPLETE,
        phase_outcome="DONE",
        run_id="run-1",
        workflow="ticket",
        phase_id=phase_id,
        phase_type="producer",
        attempt=1,
        source_artifacts=(evidence,),
        upstream=(),
        payload=_payload(),
        git_head="a" * 40,
        worktree_fingerprint="b" * 64,
    )
    return catalog, contract


def test_packaged_catalog_loads_every_unique_version() -> None:
    catalog = ContractCatalog()
    assert len(catalog.identifiers) >= 18
    assert len(catalog.identifiers) == len(set(catalog.identifiers))
    assert all(CONTRACT_SPECS_DIR.joinpath(path).is_file() for path in ["plan.json", "ci.json"])


def _example_for_schema(schema: dict[str, object]) -> object:
    """Build a non-empty representative value for every branch of the internal schema language."""
    enum = schema.get("enum")
    if isinstance(enum, list):
        return enum[0]
    kind = schema["type"]
    if kind == "object":
        properties = schema["properties"]
        required = schema.get("required", [])
        assert isinstance(properties, dict)
        assert isinstance(required, list)
        return {
            name: _example_for_schema(properties[name])
            for name in required
            if isinstance(name, str) and isinstance(properties.get(name), dict)
        }
    if kind == "array":
        items = schema["items"]
        assert isinstance(items, dict)
        count = max(1, int(schema.get("min_items", 0)))
        return [_example_for_schema(items) for _ in range(count)]
    if kind == "string":
        return "x" * max(1, int(schema.get("min_length", 0)))
    if kind in {"integer", "number"}:
        return 0
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    raise AssertionError(f"unsupported test schema kind: {kind!r}")


def test_every_packaged_spec_accepts_a_representative_payload_and_enforces_required_fields() -> None:
    catalog = ContractCatalog()
    for identifier in catalog.identifiers:
        spec = catalog.resolve(identifier)
        payload = _example_for_schema(spec.payload_schema)
        spec.validate_payload(payload)
        required = spec.payload_schema.get("required", [])
        assert isinstance(payload, dict)
        assert isinstance(required, list)
        for field in required:
            malformed = dict(payload)
            del malformed[field]
            with pytest.raises(ContractError, match="missing required fields"):
                spec.validate_payload(malformed)


@pytest.mark.parametrize(
    "identifier",
    ["", "kind", "kind/v0", "Kind/v1", "kind/v01", "kind/v-1", "kind /v1", "kind/v1 "],
)
def test_contract_identifier_rejects_ambiguous_forms(identifier: str) -> None:
    with pytest.raises(ContractError, match="invalid contract identifier"):
        parse_contract_id(identifier)


@pytest.mark.parametrize(
    ("phase_id", "encoded"),
    [
        ("review_impl.tests", "review_impl.tests"),
        ("lane/../../escape", "lane%2F..%2F..%2Fescape"),
        ("space lane", "space%20lane"),
        ("percent%lane", "percent%25lane"),
    ],
)
def test_safe_phase_id_is_reversible_and_path_safe(phase_id: str, encoded: str) -> None:
    assert safe_phase_id(phase_id) == encoded
    assert "/" not in encoded and "\\" not in encoded


@pytest.mark.parametrize("phase_id", ["", ".", ".."])
def test_safe_phase_id_rejects_degenerate_components(phase_id: str) -> None:
    with pytest.raises(ContractError):
        safe_phase_id(phase_id)


def test_canonical_json_is_stable_and_rejects_non_json_numbers() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'
    assert canonical_digest({"a": 1}) == canonical_digest({"a": 1})
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ContractError, match="not canonical JSON"):
            canonical_json({"number": value})


def test_catalog_rejects_unknown_top_level_and_schema_keys(tmp_path: Path) -> None:
    root = tmp_path / "specs"
    root.mkdir()
    base = json.loads((CONTRACT_SPECS_DIR / "plan.json").read_text(encoding="utf-8"))
    base["surprise"] = True
    (root / "bad.json").write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown keys: surprise"):
        ContractCatalog(root)

    del base["surprise"]
    base["payload"]["surprise"] = True
    (root / "bad.json").write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown schema keys: surprise"):
        ContractCatalog(root)


@pytest.mark.parametrize(
    "mutation,pattern",
    [
        (lambda data: data["payload"].update({"required": ["missing"]}), "unknown properties"),
        (lambda data: data["payload"].update({"required": ["summary", "summary"]}), "duplicate"),
        (lambda data: data["payload"].update({"additional_properties": "no"}), "must be boolean"),
        (lambda data: data["payload"].update({"min_length": -1}), "non-negative"),
    ],
)
def test_catalog_rejects_malformed_schema(
    tmp_path: Path, mutation, pattern: str
) -> None:
    root = tmp_path / "specs"
    root.mkdir()
    data = json.loads((CONTRACT_SPECS_DIR / "plan.json").read_text(encoding="utf-8"))
    mutation(data)
    (root / "bad.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ContractError, match=pattern):
        ContractCatalog(root)


def test_payload_validator_rejects_bool_as_integer_and_unknown_fields() -> None:
    spec = ContractCatalog().resolve("quill.pr-head/v1")
    with pytest.raises(ContractError, match="payload.pr must be integer"):
        spec.validate_payload({"pr": True, "expected": "a", "observed": "a", "matches": True})
    with pytest.raises(ContractError, match="unknown fields: extra"):
        spec.validate_payload(
            {"pr": 1, "expected": "a", "observed": "a", "matches": True, "extra": 1}
        )


def test_incomplete_payload_has_one_strict_nonempty_shape(tmp_path: Path) -> None:
    spec = ContractCatalog().resolve("quill.plan/v1")
    valid = {"missing": [{"field": "decisions", "reason": "ticket silent", "evidence": "#1"}]}
    contract = new_contract(
        spec=spec,
        status=ContractStatus.INCOMPLETE,
        phase_outcome="DONE",
        run_id="r",
        workflow="ticket",
        phase_id="plan",
        phase_type="producer",
        attempt=1,
        source_artifacts=(),
        upstream=(),
        payload=valid,
    )
    assert contract.contract_status is ContractStatus.INCOMPLETE

    malformed = [
        {},
        {"missing": []},
        {"missing": [{"field": "x", "reason": "y"}]},
        {
            "missing": [
                {"field": "x", "reason": "y", "evidence": "z"},
                {"field": "x", "reason": "again", "evidence": "z"},
            ]
        },
    ]
    for payload in malformed:
        with pytest.raises(ContractError):
            new_contract(
                spec=spec,
                status=ContractStatus.INCOMPLETE,
                phase_outcome="DONE",
                run_id="r",
                workflow="ticket",
                phase_id="plan",
                phase_type="producer",
                attempt=1,
                source_artifacts=(),
                upstream=(),
                payload=payload,
            )


def test_snapshot_rejects_escape_symlink_missing_and_overwrite(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-contract.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ContractError, match="escapes run directory"):
        snapshot_artifact(tmp_path, outside, "phase", 1)
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    with pytest.raises(ContractError, match="escapes run directory|non-symlink"):
        snapshot_artifact(tmp_path, link, "phase", 1)
    with pytest.raises(ContractError, match="not a regular"):
        snapshot_artifact(tmp_path, tmp_path / "missing.md", "phase", 1)

    artifact = tmp_path / "ok.md"
    artifact.write_text("ok", encoding="utf-8")
    snapshot_artifact(tmp_path, artifact, "phase", 1)
    with pytest.raises(ContractError, match="already exists"):
        snapshot_artifact(tmp_path, artifact, "phase", 1)


def test_contract_outputs_reject_symlinked_parent_directories(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    artifact = tmp_path / "artifact.md"
    artifact.write_text("evidence", encoding="utf-8")
    (tmp_path / "work").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink component"):
        snapshot_artifact(tmp_path, artifact, "phase", 1)
    assert not (outside / "phase" / "attempt-1.md").exists()

    (tmp_path / "work").unlink()
    catalog, contract = _contract(tmp_path)
    (tmp_path / "contracts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink component"):
        publish_contract(tmp_path, contract, catalog)
    assert not (outside / "research_requirements" / "attempt-1.json").exists()

    (tmp_path / "contracts").unlink()
    staged = tmp_path / "staged.json"
    staged.write_text('{"summary":"ok"}', encoding="utf-8")
    (tmp_path / "legacy").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink component"):
        publish_compatibility_view(tmp_path, staged, tmp_path / "legacy" / "result.json")
    assert not (outside / "result.json").exists()


def test_publish_is_immutable_and_latest_is_validated(tmp_path: Path) -> None:
    catalog, contract = _contract(tmp_path)
    ref = publish_contract(tmp_path, contract, catalog)
    assert ref.path == "contracts/research_requirements/attempt-1.json"
    attempt = tmp_path / ref.path
    latest = attempt.parent / "latest.json"
    assert attempt.read_bytes() == latest.read_bytes()
    loaded = load_contract(latest, catalog, run_dir=tmp_path)
    assert loaded.digest == ref.digest
    with pytest.raises(ContractError, match="already exists"):
        publish_contract(tmp_path, contract, catalog)


def test_historical_attempt_uses_immutable_snapshot_after_latest_artifact_changes(
    tmp_path: Path,
) -> None:
    catalog, first = _contract(tmp_path)
    first_ref = publish_contract(tmp_path, first, catalog)
    source = tmp_path / "research.md"
    source.write_text("# Revised research\n", encoding="utf-8")
    second_evidence = snapshot_artifact(
        tmp_path,
        source,
        "research_requirements",
        2,
    )
    second = replace(
        first,
        attempt=2,
        source_artifacts=(second_evidence,),
        digest="",
    ).with_digest()
    publish_contract(tmp_path, second, catalog)

    loaded = load_contract(tmp_path / first_ref.path, catalog, run_dir=tmp_path)
    assert loaded.attempt == 1
    assert (tmp_path / loaded.source_artifacts[0].snapshot).read_text() == "# Research\n"
    with pytest.raises(ContractError, match="artifact (size|hash) mismatch"):
        verify_artifact_ref(tmp_path, loaded.source_artifacts[0])


def test_load_rejects_envelope_payload_spec_and_digest_tampering(tmp_path: Path) -> None:
    catalog, contract = _contract(tmp_path)
    ref = publish_contract(tmp_path, contract, catalog)
    source = tmp_path / ref.path
    original = json.loads(source.read_text(encoding="utf-8"))

    mutations = [
        lambda data: data.update({"extra": True}),
        lambda data: data.update({"phase_id": "other"}),
        lambda data: data.update({"spec_digest": "0" * 64}),
        lambda data: data["payload"].update({"surprise": True}),
        lambda data: data.update({"attempt": True}),
    ]
    for index, mutation in enumerate(mutations):
        data = json.loads(json.dumps(original))
        mutation(data)
        path = tmp_path / f"tampered-{index}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ContractError):
            load_contract(path, catalog, run_dir=tmp_path, verify_artifacts=False)


def test_artifact_binding_detects_source_and_snapshot_tampering(tmp_path: Path) -> None:
    catalog, contract = _contract(tmp_path)
    artifact = contract.source_artifacts[0]
    verify_artifact_ref(tmp_path, artifact)
    (tmp_path / artifact.path).write_text("changed", encoding="utf-8")
    with pytest.raises(ContractError, match="artifact (size|hash) mismatch"):
        verify_artifact_ref(tmp_path, artifact)
    (tmp_path / artifact.path).write_text("# Research\n", encoding="utf-8")
    (tmp_path / artifact.snapshot).write_text("corrupted", encoding="utf-8")
    with pytest.raises(ContractError, match="artifact (size|hash) mismatch"):
        verify_artifact_ref(tmp_path, artifact)


def test_duplicate_and_malformed_upstream_references_are_rejected(tmp_path: Path) -> None:
    catalog, contract = _contract(tmp_path)
    reference = UpstreamContractRef(
        phase_id="upstream",
        kind="quill.plan",
        version=1,
        attempt=1,
        path="contracts/upstream/attempt-1.json",
        digest="a" * 64,
    )
    duplicate = replace(contract, upstream=(reference, reference)).with_digest()
    with pytest.raises(ContractError, match="duplicate upstream"):
        publish_contract(tmp_path, duplicate, catalog)
    malformed = replace(
        contract,
        upstream=(replace(reference, digest="not-a-digest"),),
    ).with_digest()
    with pytest.raises(ContractError, match="invalid upstream"):
        publish_contract(tmp_path, malformed, catalog)
    escaped = replace(
        contract,
        upstream=(replace(reference, path="../attempt-1.json"),),
        digest="",
    ).with_digest()
    with pytest.raises(ContractError, match="upstream contract path|upstream path"):
        publish_contract(tmp_path, escaped, catalog)


def test_contract_rejects_timezone_free_creation_timestamp(tmp_path: Path) -> None:
    catalog, contract = _contract(tmp_path)
    malformed = replace(
        contract,
        created_at="2026-08-05T12:00:00",
        digest="",
    ).with_digest()
    with pytest.raises(ContractError, match="timezone offset"):
        publish_contract(tmp_path, malformed, catalog)


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("git_head", "short", "full Git object ID"),
        ("checkpoint", "refs/heads/main", "full Git object ID"),
        ("worktree_fingerprint", "not-sha256", "must be SHA-256"),
    ],
)
def test_contract_rejects_malformed_repository_identity(
    tmp_path: Path,
    field: str,
    value: str,
    pattern: str,
) -> None:
    catalog, contract = _contract(tmp_path)
    malformed = replace(contract, **{field: value, "digest": ""}).with_digest()
    with pytest.raises(ContractError, match=pattern):
        publish_contract(tmp_path, malformed, catalog)


def test_status_and_phase_type_must_match_spec(tmp_path: Path) -> None:
    catalog = ContractCatalog()
    spec = catalog.resolve("quill.research.requirements/v1")
    with pytest.raises(ContractError, match="does not allow status"):
        new_contract(
            spec=spec,
            status=ContractStatus.UNAVAILABLE,
            phase_outcome="FAILED",
            run_id="r",
            workflow="ticket",
            phase_id="research",
            phase_type="producer",
            attempt=1,
            source_artifacts=(),
            upstream=(),
            payload=_payload(),
        )
    with pytest.raises(ContractError, match="cannot be produced"):
        new_contract(
            spec=spec,
            status=ContractStatus.COMPLETE,
            phase_outcome="PASS",
            run_id="r",
            workflow="ticket",
            phase_id="research",
            phase_type="mechanical",
            attempt=1,
            source_artifacts=(),
            upstream=(),
            payload=_payload(),
        )


def test_load_rejects_malformed_json_and_non_object(tmp_path: Path) -> None:
    catalog = ContractCatalog()
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ContractError, match="invalid contract JSON"):
        load_contract(bad, catalog)
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ContractError, match="one JSON object"):
        load_contract(bad, catalog)


def test_artifact_reference_constructor_does_not_bypass_verification(tmp_path: Path) -> None:
    fake = ArtifactRef(path="missing", snapshot="missing", sha256="0" * 64, bytes=0)
    with pytest.raises(ContractError, match="not a regular"):
        verify_artifact_ref(tmp_path, fake)
    catalog, contract = _contract(tmp_path)
    forged = replace(contract, source_artifacts=(fake,), digest="").with_digest()
    with pytest.raises(ContractError, match="snapshot path|not a regular"):
        publish_contract(tmp_path, forged, catalog)
