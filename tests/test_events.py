"""Unit tests for the event vocabulary, RunState folding, and the async bus (WI-9)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from quill import events
from quill.events import Event
from quill.phase_graph import PhaseGraph
from quill_api.events import EventBus
from quill_api.state import RunState, RunStatus

# -- event factories --------------------------------------------------------------


def test_every_event_has_type_and_ts() -> None:
    samples = [
        events.run_started("r1", 42),
        events.phase_started("0", "stash ticket"),
        events.model_loading("0", "stash ticket", "qwen"),
        events.model_load_done("0", "stash ticket", "qwen", duration_s=12.5, success=True),
        events.phase_executing("0", "stash ticket", model="qwen"),
        events.phase_done("0", "stash ticket"),
        events.gate_verdict("2", "PASS"),
        events.retry("4", 2, 2),
        events.needs_decision("which db?"),
        events.run_halted(),
        events.run_done(),
        events.run_failed(),
    ]
    for e in samples:
        assert isinstance(e["type"], str)
        assert isinstance(e["ts"], float)
    assert samples[7]["scope"] == "phase"


def test_none_payload_keys_dropped() -> None:
    e = events.run_started("r1", 42)  # repo defaults to None
    assert "repo" not in e
    e2 = events.run_started("r1", 42, repo="me/proj")
    assert e2["repo"] == "me/proj"


def test_phase_started_defaults() -> None:
    e = events.phase_started("4a", "impl review")
    assert e["phase"] == "4a"
    assert e["attempt"] == 1
    assert e["max_attempts"] == 1


def test_phase_done_carries_terminal_reason() -> None:
    event = events.phase_done("review", "review plan", verdict="GARBAGE", reason="missing receipt")
    assert event["verdict"] == "GARBAGE"
    assert event["reason"] == "missing receipt"


def test_contract_events_are_bounded_metadata_without_payloads() -> None:
    samples = [
        events.projection_started("plan", kind="quill.plan/v1", attempt=2),
        events.projection_done(
            "plan", kind="quill.plan/v1", valid=False, reason="invalid_json"
        ),
        events.contract_validated("plan", kind="quill.plan/v1", status="COMPLETE"),
        events.contract_incomplete("plan", kind="quill.plan/v1", missing_count=2),
        events.contract_published(
            "plan",
            kind="quill.plan",
            version=1,
            status="COMPLETE",
            digest="a" * 64,
            attempt=2,
        ),
    ]
    for event in samples:
        assert "payload" not in event
        assert "schema" not in event
        assert "artifact" not in event
        assert len(str(event)) < 500

    terminal = events.phase_done(
        "plan",
        "plan",
        verdict="DONE",
        contract_kind="quill.plan",
        contract_version=1,
        contract_status="COMPLETE",
        contract_digest="a" * 64,
    )
    assert terminal["contract_digest"] == "a" * 64


# -- RunState.fold_event ----------------------------------------------------------


def test_fold_run_started_sets_running() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.run_started("r1", 42, repo="me/proj"))
    assert state.status is RunStatus.RUNNING
    assert state.repo == "me/proj"


def test_fold_phase_started_tracks_phase_and_attempt() -> None:
    state = RunState(run_id="r1", ticket=42)
    event = events.phase_started("2", "plan review", attempt=1, max_attempts=2)
    state.fold_event(event)
    assert state.phase == "2"
    assert state.phase_label == "plan review"
    assert state.attempt == 1
    assert state.max_attempts == 2
    assert state.activity == "executing_phase"
    assert state.activity_label == "Executing plan review"
    assert state.phase_sequence == ["2"]
    assert state.phase_started_at == event["ts"]


def test_fold_tracks_multiple_active_phases_independently() -> None:
    state = RunState(run_id="r1", ticket=42)
    first = events.phase_started("review.architecture", "Architecture")
    second = events.phase_started("review.correctness", "Correctness")
    state.fold_event(first)
    state.fold_event(second)

    assert state.active_phases == {
        "review.architecture": first["ts"],
        "review.correctness": second["ts"],
    }

    state.fold_event(events.phase_done("review.architecture", "Architecture"))
    assert set(state.active_phases) == {"review.correctness"}


def test_fold_run_plan_stores_graph_and_projection_counts_reentry() -> None:
    graph: PhaseGraph = {
        "nodes": [
            {"id": "impl", "label": "Implement", "type": "producer", "order": 0},
            {
                "id": "review.audit",
                "label": "Audit",
                "type": "reviewer",
                "order": 1,
                "column": 1,
                "lane": 2,
                "group": "review",
            },
        ],
        "edges": [
            {
                "key": "impl->review.audit",
                "source": "impl",
                "target": "review.audit",
                "kinds": ["normal", "retry"],
            },
            {
                "key": "review.audit->impl",
                "source": "review.audit",
                "target": "impl",
                "kinds": ["retry"],
            },
        ],
    }
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.run_plan("plan", phase_graph=graph))
    for phase in ("impl", "review.audit", "impl", "review.audit"):
        state.fold_event(events.phase_started(phase, phase))
        state.fold_event(events.phase_done(phase, phase, duration_s=1.25))

    from quill_api.projections import run_summary

    summary = run_summary(state, lambda _run_id: None)
    assert summary.phase_graph is not None
    assert summary.phase_graph.nodes[1].column == 1
    assert summary.phase_graph.nodes[1].lane == 2
    assert summary.phase_graph.nodes[1].group == "review"
    assert summary.phase_route_counts == {
        "impl->review.audit": 2,
        "review.audit->impl": 1,
    }
    assert summary.phase_durations == {"impl": 2.5, "review.audit": 2.5}


def test_fold_does_not_count_fresh_parallel_attempt_as_gate_retry() -> None:
    graph: PhaseGraph = {
        "nodes": [
            {
                "id": "requirements",
                "label": "Requirements",
                "type": "producer",
                "order": 0,
                "column": 0,
                "lane": 0,
                "group": "research",
            },
            {
                "id": "technical",
                "label": "Technical",
                "type": "producer",
                "order": 1,
                "column": 0,
                "lane": 1,
                "group": "research",
            },
            {
                "id": "research_gate",
                "label": "Gate",
                "type": "reviewer",
                "order": 2,
                "column": 1,
                "lane": 0,
            },
        ],
        "edges": [
            {
                "key": "requirements->research_gate",
                "source": "requirements",
                "target": "research_gate",
                "kinds": ["normal"],
            },
            {
                "key": "technical->research_gate",
                "source": "technical",
                "target": "research_gate",
                "kinds": ["normal"],
            },
            {
                "key": "research_gate->requirements",
                "source": "research_gate",
                "target": "requirements",
                "kinds": ["retry"],
            },
            {
                "key": "research_gate->technical",
                "source": "research_gate",
                "target": "technical",
                "kinds": ["retry"],
            },
        ],
    }
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.run_plan("plan", phase_graph=graph))
    for phase in ("requirements", "technical"):
        state.fold_event(events.phase_started(phase, phase))
        state.fold_event(events.phase_done(phase, phase))
    state.fold_event(events.retry("requirements", 1, 1, scope="phase", reason="malformed"))
    state.fold_event(events.phase_started("requirements", "requirements"))
    state.fold_event(events.phase_done("requirements", "requirements"))
    state.fold_event(events.phase_started("research_gate", "research gate"))

    from quill_api.projections import run_summary

    summary = run_summary(state, lambda _run_id: None)
    assert state.phase_retry_counts == {"requirements": 1}
    assert summary.phase_route_counts["research_gate->requirements"] == 0
    assert summary.phase_route_counts["research_gate->technical"] == 0


def test_fold_model_loading_separates_internal_activity_from_configured_phase() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.phase_started("impl", "implement", model="gemma"))
    state.fold_event(events.phase_done("impl", "implement"))
    state.fold_event(events.phase_started("review", "review implementation", model="qwen"))
    state.fold_event(events.model_loading("review", "review implementation", "qwen"))

    assert state.phase == "review"
    assert state.phase_label == "review implementation"
    assert state.model == "qwen"
    assert state.active_phases == {}
    assert state.activity == "loading_model"
    assert state.activity_label == "Loading model qwen"
    assert state.phase_started_at is None
    assert state.model_loads[0].status == "active"

    state.fold_event(
        events.model_load_done(
            "review", "review implementation", "qwen", duration_s=24.5, success=True
        )
    )
    assert state.model_loads[0].status == "completed"
    assert state.model_loads[0].duration_s == 24.5
    state.fold_event(events.phase_executing("review", "review implementation", model="qwen"))
    assert state.phase == "review"
    assert set(state.active_phases) == {"review"}
    assert state.phase_started_at == state.active_phases["review"]
    assert state.activity == "executing_phase"
    assert state.activity_label == "Executing review implementation"


def test_fold_self_fix_events_projects_live_and_completed_state() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.phase_started("plan", "write plan"))
    state.fold_event(events.self_fix_started("plan", "write plan"))

    assert state.self_fixes == {"plan": "active"}
    assert state.activity_label == "Self-fixing write plan"

    state.fold_event(events.self_fix_done("plan", "write plan", repaired=True, duration_s=1.2))

    assert state.self_fixes == {"plan": "completed"}
    from quill_api.projections import run_summary

    assert run_summary(state, lambda _run_id: None).self_fixes == {"plan": "completed"}


def test_fold_contract_lifecycle_keeps_latest_attempt_metadata() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.projection_started("plan", kind="quill.plan/v1", attempt=2))
    assert state.contract_states["plan"] == {
        "phase": "plan",
        "kind": "quill.plan/v1",
        "state": "projecting",
        "attempt": 2,
    }
    state.fold_event(
        events.contract_published(
            "plan",
            kind="quill.plan",
            version=1,
            status="COMPLETE",
            digest="b" * 64,
            attempt=2,
        )
    )
    assert state.contract_states["plan"]["state"] == "published"
    assert state.contract_states["plan"]["attempt"] == 2
    assert state.contract_states["plan"]["status"] == "COMPLETE"
    assert state.contract_states["plan"]["digest"] == "b" * 64


def test_fold_gate_verdict_records_history() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.phase_started("2", "plan review"))
    state.fold_event(events.gate_verdict("2", "BLOCK", label="plan review"))
    assert len(state.history) == 1
    entry = state.history[0]
    assert entry.phase == "2"
    assert entry.verdict == "BLOCK"


def test_fold_needs_decision_parks_run() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.needs_decision("which db backend?", phase="1"))
    assert state.status is RunStatus.NEEDS_DECISION
    assert state.question == "which db backend?"
    assert state.activity == "waiting_decision"


def test_fold_phase_started_clears_stale_question() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.needs_decision("which db?"))
    state.fold_event(events.phase_started("1", "plan"))
    assert state.question is None
    assert state.status is RunStatus.RUNNING


def test_fold_run_done_sets_pr_url() -> None:
    state = RunState(run_id="r1", ticket=42)
    state.fold_event(events.run_done(pr_url="https://github.com/me/proj/pull/9"))
    assert state.status is RunStatus.DONE
    assert state.pr_url == "https://github.com/me/proj/pull/9"


def test_fold_unknown_event_ignored() -> None:
    state = RunState(run_id="r1", ticket=42)
    before = state.status
    state.fold_event({"type": "some_future_event", "ts": 1.0})
    assert state.status is before


# -- acceptance: recording on_event drives the full sequence ----------------------


def _fake_run(on_event: Callable[[Event], None]) -> None:
    """Stand-in for run_pipeline (WI-4 not implemented): emit a canonical happy-path run.

    Phases: 0 setup -> 1 plan -> 2 plan-review PASS -> 3 impl -> done.
    """
    on_event(events.run_started("r1", 42, repo="me/proj"))
    on_event(events.phase_started("0", "setup"))
    on_event(events.phase_done("0", "setup"))
    on_event(events.phase_started("1", "plan"))
    on_event(events.phase_done("1", "plan"))
    on_event(events.phase_started("2", "plan review", max_attempts=2))
    on_event(events.gate_verdict("2", "PASS", label="plan review"))
    on_event(events.phase_started("3", "implement"))
    on_event(events.phase_done("3", "implement"))
    on_event(events.run_done(pr_url="https://github.com/me/proj/pull/9"))


def test_acceptance_event_sequence_and_final_state() -> None:
    recorded: list[Event] = []
    state = RunState(run_id="r1", ticket=42)

    def on_event(e: Event) -> None:
        recorded.append(e)
        state.fold_event(e)

    _fake_run(on_event)

    # Event sequence.
    assert [e["type"] for e in recorded] == [
        events.RUN_STARTED,
        events.PHASE_STARTED,
        events.PHASE_DONE,
        events.PHASE_STARTED,
        events.PHASE_DONE,
        events.PHASE_STARTED,
        events.GATE_VERDICT,
        events.PHASE_STARTED,
        events.PHASE_DONE,
        events.RUN_DONE,
    ]

    # Final state.
    assert state.status is RunStatus.DONE
    assert state.repo == "me/proj"
    assert state.phase == "3"
    assert state.pr_url == "https://github.com/me/proj/pull/9"
    # history captured the phase_done + gate verdicts.
    assert [(h.phase, h.verdict) for h in state.history] == [
        ("0", None),
        ("1", None),
        ("2", "PASS"),
        ("3", None),
    ]


# -- EventBus ---------------------------------------------------------------------


async def _take(bus: EventBus, n: int) -> list[Event]:
    out: list[Event] = []
    async for e in bus.subscribe():
        out.append(e)
        if len(out) == n:
            break
    return out


async def test_bus_fans_out_to_multiple_subscribers() -> None:
    bus = EventBus()
    # Start two subscribers, give them a tick to register their queues.
    t1 = asyncio.create_task(_take(bus, 2))
    t2 = asyncio.create_task(_take(bus, 2))
    await asyncio.sleep(0)

    bus.publish(events.run_started("r1", 42))
    bus.publish(events.run_done())

    r1, r2 = await asyncio.gather(t1, t2)
    assert [e["type"] for e in r1] == [events.RUN_STARTED, events.RUN_DONE]
    assert [e["type"] for e in r2] == [events.RUN_STARTED, events.RUN_DONE]


async def test_bus_subscriber_count_tracks_lifecycle() -> None:
    bus = EventBus()
    assert bus.subscriber_count == 0
    task = asyncio.create_task(_take(bus, 1))
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    bus.publish(events.run_done())
    await task
    # The subscriber's queue is discarded when its generator closes (finally), which
    # runs as the task tears down — yield the loop a tick so cleanup completes.
    await asyncio.sleep(0)
    assert bus.subscriber_count == 0


async def test_publish_threadsafe_requires_bound_loop() -> None:
    bus = EventBus()
    with pytest.raises(RuntimeError, match="loop not bound"):
        bus.publish_threadsafe(events.run_done())


async def test_publish_threadsafe_delivers_from_thread() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(_take(bus, 1))
    await asyncio.sleep(0)

    # Emulate the driver worker thread.
    await asyncio.to_thread(bus.publish_threadsafe, events.run_started("r1", 7))
    got = await task
    assert got[0]["type"] == events.RUN_STARTED
    assert got[0]["ticket"] == 7
