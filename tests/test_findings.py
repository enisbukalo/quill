from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from quill.findings import (
    BLOCKING_SEVERITIES,
    DEFAULT_BLOCKING_POLICY,
    BlockingPolicy,
    Finding,
    deterministic_gate_result,
    deterministic_review_result,
    load_findings,
    materialize_verification_delta,
    merge_verification_findings,
)
from quill.phases import Outcome, PhaseResult


def _write(path: Path, findings: list[dict[str, str]]) -> None:
    path.write_text(json.dumps({"schema_version": 1, "findings": findings}), encoding="utf-8")


def _finding(**overrides: str) -> dict[str, str]:
    row = {
        "id": "F1",
        "severity": "MAJOR",
        "status": "OPEN",
        "title": "Required callback is invalid",
        "requirement": "Use a supported callback",
        "evidence": "main.gd:12 calls an unsupported API",
        "failure_scenario": "Initialization never executes",
        "required_outcome": "Use a supported lifecycle callback",
    }
    row.update(overrides)
    return row


def test_open_major_blocks_even_when_model_receipt_says_pass(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "looks good"))

    assert result.outcome is Outcome.BLOCK
    assert "F1 (MAJOR)" in result.message


def test_only_advisory_findings_pass_even_when_model_receipt_says_block(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(severity="MINOR")])

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.BLOCK, "blocked"))

    assert result.outcome is Outcome.PASS


def test_malformed_findings_are_garbage(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    artifact.write_text('{"findings": []}', encoding="utf-8")

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "looks good"))

    assert result.outcome is Outcome.GARBAGE
    assert "schema_version 1" in result.message


def test_selective_gate_requires_valid_owner_for_blocker(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(owner="wrong-lane")])

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.BLOCK, "blocked"),
        allowed_owners=("requirements", "technical"),
    )

    assert result.outcome is Outcome.GARBAGE
    assert "must name owner from" in result.message


def test_verification_new_blocker_requires_revision_origin(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    prior = load_findings(artifact)
    _write(
        artifact,
        [
            _finding(status="RESOLVED"),
            _finding(id="F2", title="Late discovery"),
        ],
    )

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.BLOCK, "blocked"), prior=prior)

    assert result.outcome is Outcome.GARBAGE
    assert "introduced_by_revision" in result.message


def test_verification_cannot_omit_prior_blocker(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    initial = deterministic_gate_result(artifact, PhaseResult(Outcome.BLOCK, "blocked"))
    assert initial.outcome is Outcome.BLOCK
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(artifact, [])

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.GARBAGE
    assert "omitted prior blocking finding(s): F1" in result.message


def test_verification_accepts_evidence_backed_resolution(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(artifact, [_finding(status="RESOLVED", evidence="main.gd:12 uses _ready")])

    result = deterministic_gate_result(
        artifact, PhaseResult(Outcome.BLOCK, "still blocked"), prior=prior
    )

    assert result.outcome is Outcome.PASS


def test_verification_accepts_unique_namespaced_resolution_id(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(
        artifact,
        [
            _finding(
                id="prev-F1",
                status="RESOLVED",
                evidence="main.gd:12 now uses _ready and the lifecycle test passes",
            ),
            _finding(id="arch-F1", severity="MINOR", title="Advisory follow-up"),
        ],
    )

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.PASS


def test_exact_id_takes_precedence_over_resolved_alias(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(artifact, [_finding(), _finding(id="prev-F1", status="RESOLVED")])

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.BLOCK
    assert "F1 (MAJOR)" in result.message


def test_verification_rejects_ambiguous_resolution_aliases(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(
        artifact,
        [
            _finding(id="prev-F1", status="RESOLVED"),
            _finding(id="resolved-F1", status="RESOLVED"),
        ],
    )

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.GARBAGE
    assert "ambiguously renamed" in result.message
    assert "prev-F1, resolved-F1" in result.message


def test_verification_preserves_open_namespaced_blocker(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(
        artifact,
        [
            _finding(id="prev-F1", status="OPEN"),
            _finding(id="resolved-F1", status="RESOLVED", severity="MINOR"),
        ],
    )

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.BLOCK
    assert "prev-F1 (MAJOR)" in result.message


def test_verification_rejects_non_segment_suffix_alias(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    from quill.findings import load_findings

    prior = load_findings(artifact)
    _write(artifact, [_finding(id="notF1", status="RESOLVED")])

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.GARBAGE
    assert "omitted prior blocking finding(s): F1" in result.message


def test_verification_rejects_reused_id_for_different_finding(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])
    prior = load_findings(artifact)
    _write(
        artifact,
        [
            _finding(
                status="RESOLVED",
                title="Different historical defect",
                requirement="A different requirement",
            )
        ],
    )

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=prior)

    assert result.outcome is Outcome.GARBAGE
    assert "changed identity field(s)" in result.message
    assert "title, requirement" in result.message


def test_verification_merge_assigns_stable_revision_id_to_reused_identity() -> None:
    prior = Finding(
        **_finding(
            id="tests:dead-code-runtime-context",
            title="Three test functions are never executed",
            requirement="Execute the three integration tests",
            failure_scenario="Three integration paths go untested",
            required_outcome="Call all three tests from run()",
        )
    )
    current = Finding(
        **_finding(
            id="tests:dead-code-runtime-context",
            title="Five test functions are never executed",
            requirement="Execute the five additional integration tests",
            failure_scenario="Five additional integration paths go untested",
            required_outcome="Call all five additional tests from run()",
        )
    )

    first = merge_verification_findings((prior,), (current,))
    second = merge_verification_findings((prior,), (current,))

    assert first == second
    assert len(first) == 2
    assert first[0] == prior
    assert first[1].id.startswith("tests:dead-code-runtime-context:revision-")
    assert first[1].title == current.title
    assert len({finding.id for finding in first}) == 2


def test_verification_merge_collapses_exact_duplicate_and_keeps_it_open() -> None:
    prior = Finding(**_finding(evidence="original evidence"))
    resolved = Finding(**_finding(status="RESOLVED", evidence="new resolution evidence"))

    merged = merge_verification_findings((prior,), (resolved, resolved))

    assert len(merged) == 1
    assert merged[0].status == "OPEN"
    assert merged[0].evidence == "new resolution evidence"


def test_reused_id_revision_remains_an_independent_blocker(tmp_path: Path) -> None:
    prior = Finding(**_finding(title="Original defect"))
    current = Finding(
        **_finding(
            title="New defect",
            requirement="A new requirement",
            failure_scenario="A new path fails",
            required_outcome="Fix the new path",
        )
    )
    merged = merge_verification_findings((prior,), (current,))
    artifact = tmp_path / "review.md"
    _write(
        artifact,
        [
            asdict(replace(merged[0], status="RESOLVED")),
            asdict(merged[1]),
        ],
    )

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "fixed"), prior=merged)

    assert result.outcome is Outcome.BLOCK
    assert merged[1].id in result.message
    assert "New defect" in result.message


def test_valid_artifact_decides_gate_when_receipt_is_garbage(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])

    result = deterministic_gate_result(
        artifact, PhaseResult(Outcome.GARBAGE, "no receipt in worker output")
    )

    assert result.outcome is Outcome.BLOCK


def test_explicit_process_failure_remains_authoritative(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [])

    result = deterministic_gate_result(artifact, PhaseResult(Outcome.FAILED, "could not review"))

    assert result.outcome is Outcome.FAILED


def test_informational_review_namespaces_ids_deterministically(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])

    result = deterministic_review_result(
        artifact,
        PhaseResult(Outcome.GARBAGE, "invalid receipt"),
        namespace="architecture",
    )

    assert result.outcome is Outcome.DONE
    assert load_findings(artifact)[0].id == "architecture:F1"


# -- round-aware blocking policy --------------------------------------------------

_CONVERGING = BlockingPolicy(
    initial=BLOCKING_SEVERITIES,
    retry_mode="repeat-only",
    final=frozenset({"CRITICAL"}),
)


def test_initial_round_blocks_on_major_under_converging_policy(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding()])

    result = deterministic_gate_result(
        artifact, PhaseResult(Outcome.PASS, "ok"), policy=_CONVERGING, round_index=0
    )

    assert result.outcome is Outcome.BLOCK


def test_retry_round_makes_newly_discovered_major_advisory(tmp_path: Path) -> None:
    """The ticket #19 treadmill: a fresh MAJOR nobody reported before must not block a retry."""
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="ARCH-REG-002")])

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.BLOCK, "blocked"),
        policy=_CONVERGING,
        round_index=1,
        carried_ids=frozenset({"ARCH-REG-001"}),
    )

    assert result.outcome is Outcome.PASS


def test_retry_round_still_blocks_on_repeated_major(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="ARCH-REG-001")])

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.PASS, "ok"),
        policy=_CONVERGING,
        round_index=1,
        carried_ids=frozenset({"ARCH-REG-001"}),
    )

    assert result.outcome is Outcome.BLOCK
    assert "ARCH-REG-001" in result.message


def test_retry_round_treats_namespaced_repeat_as_repeat(tmp_path: Path) -> None:
    """Lane namespacing rewrites F1 to tests:F1 between rounds; that is still the same defect."""
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="tests:F1")])

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.PASS, "ok"),
        policy=_CONVERGING,
        round_index=1,
        carried_ids=frozenset({"F1"}),
    )

    assert result.outcome is Outcome.BLOCK


def test_critical_blocks_on_every_round_including_the_final_one(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="NEW-CRIT", severity="CRITICAL")])

    for round_index, final in ((1, False), (3, True)):
        result = deterministic_gate_result(
            artifact,
            PhaseResult(Outcome.PASS, "ok"),
            policy=_CONVERGING,
            round_index=round_index,
            carried_ids=frozenset({"SOMETHING-ELSE"}),
            final_round=final,
        )
        assert result.outcome is Outcome.BLOCK, f"round {round_index}"


def test_final_round_drops_major_entirely(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="F1")])

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.BLOCK, "blocked"),
        policy=_CONVERGING,
        round_index=3,
        carried_ids=frozenset({"F1"}),
        final_round=True,
    )

    assert result.outcome is Outcome.PASS


def test_advisory_prior_finding_is_not_demanded_back(tmp_path: Path) -> None:
    """Relaxing the gate must not re-enter through the identity-preservation check."""
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="CARRIED")])
    prior = (
        Finding(**_finding(id="CARRIED")),
        Finding(**_finding(id="LATE-DISCOVERY")),
    )

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.PASS, "ok"),
        prior=prior,
        policy=_CONVERGING,
        round_index=1,
        carried_ids=frozenset({"CARRIED"}),
    )

    # LATE-DISCOVERY is absent from the artifact but advisory this round, so it is not "omitted".
    assert result.outcome is Outcome.BLOCK
    assert "CARRIED" in result.message
    assert "omitted" not in result.message


def test_blocking_set_strictly_shrinks_across_rounds(tmp_path: Path) -> None:
    """The convergence property: each round can only ever drop blockers, never add them."""
    artifact = tmp_path / "review.md"
    carried = frozenset({"F1", "F2", "F3"})
    open_ids = ["F1", "F2", "F3"]
    sizes: list[int] = []

    for round_index in range(1, 4):
        # Each round resolves one prior blocker and the reviewers invent a brand-new MAJOR.
        open_ids = open_ids[1:]
        rows = [_finding(id=fid) for fid in open_ids]
        rows.append(_finding(id=f"LATE-{round_index}"))
        _write(artifact, rows)

        result = deterministic_gate_result(
            artifact,
            PhaseResult(Outcome.BLOCK, "blocked"),
            policy=_CONVERGING,
            round_index=round_index,
            carried_ids=carried,
        )
        blocking = 0 if result.outcome is Outcome.PASS else result.message.count("(MAJOR)")
        sizes.append(blocking)

    assert sizes == [2, 1, 0]


def test_default_policy_preserves_historic_behavior(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    _write(artifact, [_finding(id="BRAND-NEW")])

    result = deterministic_gate_result(
        artifact,
        PhaseResult(Outcome.PASS, "ok"),
        policy=DEFAULT_BLOCKING_POLICY,
        round_index=5,
        carried_ids=frozenset({"UNRELATED"}),
        final_round=True,
    )

    assert result.outcome is Outcome.BLOCK


# -- status-delta verification contract -------------------------------------------


def _prior_pair() -> tuple[Finding, ...]:
    return (
        Finding(**_finding(id="architecture:F1")),
        Finding(**_finding(id="tests:F2")),
    )


def _write_delta(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps({"schema_version": 1, **payload}), encoding="utf-8")


def test_delta_applies_dispositions_onto_carried_findings(tmp_path: Path) -> None:
    artifact = tmp_path / "final.json"
    _write_delta(
        artifact,
        {
            "dispositions": [
                {"id": "architecture:F1", "status": "RESOLVED", "evidence": "app.gd:14 now guards"}
            ]
        },
    )

    materialize_verification_delta(artifact, _prior_pair())
    findings = load_findings(artifact)

    assert [(f.id, f.status) for f in findings] == [
        ("architecture:F1", "RESOLVED"),
        ("tests:F2", "OPEN"),
    ]
    # Identity is reassembled from Quill's copy, so it cannot drift; evidence is the model's.
    assert findings[0].title == "Required callback is invalid"
    assert findings[0].evidence == "app.gd:14 now guards"


def test_delta_leaves_undisposed_prior_findings_open(tmp_path: Path) -> None:
    """Silence is not omission: Quill holds the record, so no claim means no change."""
    artifact = tmp_path / "final.json"
    _write_delta(artifact, {"dispositions": []})

    materialize_verification_delta(artifact, _prior_pair())

    assert [(f.id, f.status) for f in load_findings(artifact)] == [
        ("architecture:F1", "OPEN"),
        ("tests:F2", "OPEN"),
    ]


def test_delta_admits_new_findings(tmp_path: Path) -> None:
    artifact = tmp_path / "final.json"
    _write_delta(
        artifact,
        {
            "dispositions": [{"id": "architecture:F1", "status": "RESOLVED", "evidence": "fixed"}],
            "new_findings": [
                _finding(id="REG-1", introduced_by_revision="the new guard clause in app.gd")
            ],
        },
    )

    materialize_verification_delta(artifact, _prior_pair())
    findings = load_findings(artifact)

    assert [f.id for f in findings] == ["architecture:F1", "tests:F2", "REG-1"]
    assert findings[-1].introduced_by_revision == "the new guard clause in app.gd"


def test_delta_rejects_a_disposition_for_an_unknown_finding(tmp_path: Path) -> None:
    artifact = tmp_path / "final.json"
    _write_delta(artifact, {"dispositions": [{"id": "INVENTED", "status": "RESOLVED"}]})

    with pytest.raises(ValueError, match="names unknown finding"):
        materialize_verification_delta(artifact, _prior_pair())


def test_delta_rejects_an_invalid_status(tmp_path: Path) -> None:
    artifact = tmp_path / "final.json"
    _write_delta(artifact, {"dispositions": [{"id": "tests:F2", "status": "PROBABLY"}]})

    with pytest.raises(ValueError, match="invalid status"):
        materialize_verification_delta(artifact, _prior_pair())


def test_full_array_verification_artifact_passes_through_untouched(tmp_path: Path) -> None:
    """A model that ignores the delta shape must still work."""
    artifact = tmp_path / "final.json"
    _write(artifact, [_finding(id="architecture:F1"), _finding(id="tests:F2")])
    before = artifact.read_text(encoding="utf-8")

    materialize_verification_delta(artifact, _prior_pair())

    assert artifact.read_text(encoding="utf-8") == before


def test_delta_cannot_express_the_identity_drift_that_used_to_discard_runs(tmp_path: Path) -> None:
    """The ticket #19 killer: a re-review that reworded a prior finding returned GARBAGE.

    Under the delta contract the model never re-emits those fields, so the whole
    ``verification changed identity field(s)`` failure class is unreachable.
    """
    prior = _prior_pair()
    artifact = tmp_path / "final.json"
    _write_delta(
        artifact,
        {
            "dispositions": [
                # The model "rewords" — but a disposition has nowhere to put a title or severity.
                {"id": "architecture:F1", "status": "OPEN", "evidence": "still broken, reworded"},
                {"id": "tests:F2", "status": "RESOLVED", "evidence": "covered by a new test"},
            ]
        },
    )

    materialize_verification_delta(artifact, prior)
    result = deterministic_gate_result(artifact, PhaseResult(Outcome.PASS, "ok"), prior=prior)

    assert result.outcome is Outcome.BLOCK
    assert "architecture:F1" in result.message
    assert "identity" not in result.message
    assert "omitted" not in result.message
