"""Restart lineage preserves evidence without allowing stale or unsafe files."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from quill.config import load_config
from quill.contracts import (
    ContractStatus,
    default_catalog,
    new_contract,
    publish_contract,
    snapshot_artifact,
    upstream_ref,
)
from quill.eventlog import EventLog
from quill.restart import (
    RestartError,
    model_overrides,
    prepare_contract_restart,
    restart_contract_refs,
    seed_events,
    seed_transcripts,
    write_seed,
)
from quill.telemetry import build_breakdown
from quill_api.projections import run_summary
from quill_api.state import RunState, RunStatus


def _execution(
    sequence: int,
    phase: str,
    *,
    started_at: float,
    finished_at: float,
    transcript: str,
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "phase": phase,
        "label": phase.title(),
        "call_number": 1,
        "phase_type": "producer",
        "model": "model-35b",
        "verdict": "DONE",
        "rejection_reason": None,
        "self_check_status": "passed" if phase == "plan" else "not_run",
        "self_check_duration_s": 0.25 if phase == "plan" else None,
        "self_fix_status": "not_run",
        "self_fix_duration_s": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": finished_at - started_at,
        "tool_calls_by_name": {"read": sequence},
        "transcripts": [transcript],
    }


def _stream(path: Path, *, session_id: str, timestamp: float, input_tokens: int) -> None:
    path.write_text(
        "\n".join(
            (
                json.dumps({"type": "session", "id": session_id}),
                json.dumps(
                    {
                        "type": "message_end",
                        "timestamp": timestamp,
                        "message": {
                            "timestamp": timestamp,
                            "model": "model-35b",
                            "provider": "vllm",
                            "usage": {
                                "input": input_tokens,
                                "output": 10,
                                "totalTokens": input_tokens + 10,
                                "cost": {"total": 0},
                            },
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_restart_lineage_replays_graph_history_and_usage(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    source_plan = {
        "type": "run_plan",
        "ts": 0.5,
        "phase_graph": {
            "nodes": [
                {"id": "plan", "label": "Plan", "type": "producer", "order": 0},
                {"id": "test", "label": "Test", "type": "mechanical", "order": 1},
            ],
            "edges": [
                {
                    "key": "plan->test",
                    "source": "plan",
                    "target": "test",
                    "kinds": ["normal"],
                }
            ],
        },
    }
    (source / "state.jsonl").write_text(json.dumps(source_plan) + "\n", encoding="utf-8")
    executions = [
        _execution(
            1,
            "plan",
            started_at=1.0,
            finished_at=2.0,
            transcript="stream-plan-model-1.jsonl",
        ),
        _execution(
            2,
            "test",
            started_at=3.0,
            finished_at=4.0,
            transcript="stream-test-model-1.jsonl",
        ),
    ]
    _stream(
        source / "stream-plan-model-1.jsonl",
        session_id="plan-session",
        timestamp=1.5,
        input_tokens=100,
    )
    _stream(
        source / "stream-test-model-1.jsonl",
        session_id="test-session",
        timestamp=3.5,
        input_tokens=200,
    )

    inherited = seed_events("source", source, executions)
    write_seed(
        target,
        source_run_id="source",
        source_sequence=3,
        phase="build",
        start_phase="build",
        executions=executions,
        phase_set_hash="hash-v1",
        checkpoint="checkpoint-1",
    )
    for name in seed_transcripts(target):
        shutil.copy2(source / name, target / name)
    with EventLog(target) as event_log:
        for event in inherited:
            event_log.append(event)

    state = RunState(run_id="target", ticket=1, status=RunStatus.QUEUED)
    for event in inherited:
        state.fold_event(event)
    state.status = RunStatus.QUEUED
    state.active_phases.clear()
    summary = run_summary(state, lambda _run_id: None)
    breakdown = build_breakdown("target", target, {"status": "queued", "backend": "vllm"})

    assert [item["phase"] for item in breakdown["phase_executions"]] == ["plan", "test"]
    assert breakdown["cumulative_usage"]["total_tokens"] == 320
    assert breakdown["phase_executions"][0]["self_check_status"] == "passed"
    assert summary.phase_route_counts == {"plan->test": 1}


def test_restart_models_prefer_observed_execution_and_seed_paths_are_safe(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "state.jsonl").write_text(
        json.dumps(
            {
                "type": "run_queued",
                "model_overrides": {"plan": "model-27b", "later": "model-35b"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    executions = [
        {
            "phase": "plan",
            "model": "model-35b",
            "transcripts": ["stream-plan-model-1.jsonl", "../outside.jsonl"],
        }
    ]

    assert model_overrides(source, executions) == {
        "plan": "model-35b",
        "later": "model-35b",
    }
    write_seed(
        target,
        source_run_id="source",
        source_sequence=2,
        phase="test",
        start_phase="test",
        executions=executions,
        phase_set_hash="hash-v1",
        checkpoint="checkpoint-1",
    )
    assert seed_transcripts(target) == {"stream-plan-model-1.jsonl"}


def _restart_config(root: Path):
    personas = root / "personas"
    personas.mkdir(parents=True)
    for name in ("branch.md", "plan.md", "impl.md"):
        (personas / name).write_text("persona", encoding="utf-8")
    (root / "quillfolio.toml").write_text(
        """
[repo]
name = "me/repo"

[runner]
kind = "opencode"

[build]
command = "true"
test = "true"

[[phase]]
id = "branch"
type = "producer"
persona = "branch.md"
model = "model"
artifact = "branch.md"
produces_contract = "quill.artifact/v1"

[[phase]]
id = "plan"
type = "producer"
persona = "plan.md"
model = "model"
artifact = "plan.md"
inputs = ["branch"]
produces_contract = "quill.plan/v1"
accepts_contracts = ["quill.artifact/v1"]

[[phase]]
id = "impl"
type = "producer"
persona = "impl.md"
model = "model"
artifact = "impl.md"
inputs = ["plan"]
produces_contract = "quill.implementation/v1"
accepts_contracts = ["quill.plan/v1"]
""",
        encoding="utf-8",
    )
    return load_config(root, personas_root=personas)


def _publish_restart_contracts(source: Path) -> tuple[str, str]:
    catalog = default_catalog()
    (source / "branch.md").write_text("branch evidence", encoding="utf-8")
    branch_artifact = snapshot_artifact(source, source / "branch.md", "branch", 1)
    branch = new_contract(
        spec=catalog.resolve("quill.artifact/v1"),
        status=ContractStatus.COMPLETE,
        phase_outcome="DONE",
        run_id="source",
        workflow="ticket",
        phase_id="branch",
        phase_type="producer",
        attempt=1,
        source_artifacts=(branch_artifact,),
        upstream=(),
        payload={
            "summary": "branch",
            "outputs": [],
            "verification": [],
            "unknowns": [],
            "obligations": [],
        },
    )
    branch_ref = publish_contract(source, branch, catalog)
    (source / "plan.md").write_text("plan evidence", encoding="utf-8")
    plan_artifact = snapshot_artifact(source, source / "plan.md", "plan", 1)
    plan = new_contract(
        spec=catalog.resolve("quill.plan/v1"),
        status=ContractStatus.COMPLETE,
        phase_outcome="DONE",
        run_id="source",
        workflow="ticket",
        phase_id="plan",
        phase_type="producer",
        attempt=1,
        source_artifacts=(plan_artifact,),
        upstream=(upstream_ref(branch_ref),),
        payload={
            "summary": "plan",
            "decisions": [],
            "phases": ["implement"],
            "evidence": ["plan evidence"],
            "verification": [],
            "unknowns": [],
        },
    )
    plan_ref = publish_contract(source, plan, catalog)
    return branch_ref.path, plan_ref.path


def _write_contract_seed(target: Path, config_hash: str) -> None:
    write_seed(
        target,
        source_run_id="source",
        source_sequence=3,
        phase="impl",
        start_phase="impl",
        executions=[],
        phase_set_hash=config_hash,
        checkpoint="checkpoint-3",
    )


def test_restart_copies_only_validated_transitive_contract_closure(tmp_path: Path) -> None:
    config = _restart_config(tmp_path / "repo")
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    branch_path, plan_path = _publish_restart_contracts(source)
    (source / "unrelated.txt").write_text("must not copy", encoding="utf-8")
    (source / "stream-impl-model-1.jsonl").write_text("{}\n", encoding="utf-8")
    _write_contract_seed(target, config.phase_set_hash())

    refs = prepare_contract_restart(
        source,
        target,
        config=config,
        start_phase="impl",
        source_run_id="source",
        checkpoint="checkpoint-3",
    )

    assert set(refs) == {"branch", "plan"}
    assert (target / branch_path).is_file()
    assert (target / plan_path).is_file()
    assert not (target / "unrelated.txt").exists()
    assert restart_contract_refs(target, config=config, start_phase="impl") == refs


def test_restart_rejects_symlinked_target_parent_without_writing_outside(tmp_path: Path) -> None:
    config = _restart_config(tmp_path / "repo")
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    _publish_restart_contracts(source)
    _write_contract_seed(target, config.phase_set_hash())
    (target / "contracts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RestartError, match="symlink component"):
        prepare_contract_restart(
            source,
            target,
            config=config,
            start_phase="impl",
            source_run_id="source",
            checkpoint="checkpoint-3",
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("damage", ["artifact", "contract", "seed"])
def test_restart_rejects_tampered_or_partial_closure(tmp_path: Path, damage: str) -> None:
    config = _restart_config(tmp_path / "repo")
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    _publish_restart_contracts(source)
    _write_contract_seed(target, config.phase_set_hash())
    if damage == "artifact":
        (source / "work" / "branch" / "attempt-1.md").write_text("tampered", encoding="utf-8")
        with pytest.raises(RestartError, match="invalid latest restart contract|artifact"):
            prepare_contract_restart(
                source,
                target,
                config=config,
                start_phase="impl",
                source_run_id="source",
                checkpoint="checkpoint-3",
            )
        return

    prepare_contract_restart(
        source,
        target,
        config=config,
        start_phase="impl",
        source_run_id="source",
        checkpoint="checkpoint-3",
    )
    if damage == "contract":
        path = target / "contracts" / "plan" / "attempt-1.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["payload"]["summary"] = "tampered"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(RestartError, match="entry does not match|invalid inherited"):
            restart_contract_refs(target, config=config, start_phase="impl")
    else:
        seed_path = target / "restart-lineage.json"
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        seed["contracts"] = seed["contracts"][:-1]
        seed_path.write_text(json.dumps(seed), encoding="utf-8")
        with pytest.raises(
            RestartError, match="artifact inventory|missing upstream|missing direct"
        ):
            restart_contract_refs(target, config=config, start_phase="impl")
