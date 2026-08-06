import json
from pathlib import Path

from quill import events
from quill.config import AuditDef, PhaseDef
from quill.phase_graph import build_phase_graph, route_counts
from quill_api.routers.runs import _historical_graph, _historical_model_loads


def test_build_phase_graph_deduplicates_normal_and_retry_edges() -> None:
    phases = [
        PhaseDef(id="plan", type="producer", label="Plan"),
        PhaseDef(id="impl", type="producer", label="Implement"),
        PhaseDef(
            id="review",
            type="reviewer",
            label="Review",
            gates=True,
            on_block=("impl",),
        ),
        PhaseDef(id="ship", type="mechanical", label="Ship"),
    ]

    graph = build_phase_graph(phases)

    assert [node["id"] for node in graph["nodes"]] == ["plan", "impl", "review", "ship"]
    edges = {edge["key"]: edge for edge in graph["edges"]}
    assert edges["impl->review"]["kinds"] == ["normal"]
    assert edges["review->impl"]["kinds"] == ["retry"]
    assert edges["review->ship"]["kinds"] == ["normal"]


def test_route_counts_repeated_declared_transitions_and_zero_routes() -> None:
    graph = build_phase_graph(
        [
            PhaseDef(id="impl", type="producer"),
            PhaseDef(id="review", type="reviewer", gates=True, on_block=("impl",)),
            PhaseDef(id="ship", type="mechanical"),
        ]
    )

    counts = route_counts(graph, ["impl", "review", "impl", "review", "ship"])

    assert counts == {
        "impl->review": 2,
        "review->impl": 1,
        "review->ship": 1,
    }


def test_route_counts_do_not_present_local_parallel_retry_as_gate_back_edge() -> None:
    graph = build_phase_graph(
        [
            PhaseDef(id="requirements", type="producer", parallel_group="research"),
            PhaseDef(id="technical", type="producer", parallel_group="research"),
            PhaseDef(
                id="research_gate",
                type="reviewer",
                gates=True,
                selective_on_block=("requirements", "technical"),
            ),
        ]
    )

    counts = route_counts(
        graph,
        ["requirements", "technical", "requirements", "research_gate"],
        phase_retries={"requirements": 1},
    )

    assert counts["research_gate->requirements"] == 0
    assert counts["research_gate->technical"] == 0


def test_single_phase_has_no_fabricated_routes() -> None:
    graph = build_phase_graph([PhaseDef(id="plan", type="producer")])
    assert graph["edges"] == []
    assert route_counts(graph, ["plan"]) == {}


def test_phase_graph_marks_configured_self_check_without_fabricating_route() -> None:
    graph = build_phase_graph(
        [
            PhaseDef(id="plan", type="producer", self_check=True),
            PhaseDef(id="review", type="reviewer"),
        ]
    )

    assert graph["nodes"][0]["self_check"] is True
    assert graph["nodes"][1]["self_check"] is False
    assert graph["nodes"][0]["self_fix"] is True
    assert graph["nodes"][1]["self_fix"] is True
    assert [edge["key"] for edge in graph["edges"]] == ["plan->review"]


def test_phase_graph_disables_self_fix_for_mechanical_phases() -> None:
    graph = build_phase_graph(
        [PhaseDef(id="plan", type="producer"), PhaseDef(id="test", type="mechanical")]
    )

    assert graph["nodes"][0]["self_fix"] is True
    assert graph["nodes"][1]["self_fix"] is False


def test_concurrent_audits_expand_into_parallel_nodes_with_fan_in_and_out() -> None:
    audits = tuple(
        AuditDef(name, name.title(), f"{name}.md", "qwen")
        for name in ("architecture", "correctness", "tests")
    )
    graph = build_phase_graph(
        [
            PhaseDef(id="impl", type="producer"),
            PhaseDef(id="review_impl", type="reviewer", audits=audits),
            PhaseDef(id="implementation_gate", type="finalizer"),
        ]
    )

    audit_nodes = [node for node in graph["nodes"] if node.get("group") == "review_impl"]
    assert {node["column"] for node in audit_nodes} == {1}
    assert [node["lane"] for node in audit_nodes] == [0, 1, 2]
    edges = {edge["key"] for edge in graph["edges"]}
    for node in audit_nodes:
        assert f"impl->{node['id']}" in edges
        assert f"{node['id']}->implementation_gate" in edges

    sequence = ["impl", *(node["id"] for node in audit_nodes), "implementation_gate"]
    counts = route_counts(graph, sequence)
    assert all(counts[f"impl->{node['id']}"] == 1 for node in audit_nodes)
    assert all(counts[f"{node['id']}->implementation_gate"] == 1 for node in audit_nodes)


def test_parallel_producers_share_one_graph_column_and_selective_retry_edges() -> None:
    phases = [
        PhaseDef(id="requirements", type="producer", parallel_group="research"),
        PhaseDef(id="architecture", type="producer", parallel_group="research"),
        PhaseDef(id="technical", type="producer", parallel_group="research"),
        PhaseDef(id="synthesis", type="producer"),
        PhaseDef(
            id="research_gate",
            type="reviewer",
            gates=True,
            on_block=("synthesis",),
            selective_on_block=("requirements", "architecture", "technical"),
        ),
    ]

    graph = build_phase_graph(phases)

    lanes = [node for node in graph["nodes"] if node.get("group") == "research"]
    assert {node["column"] for node in lanes} == {0}
    assert [node["lane"] for node in lanes] == [0, 1, 2]
    edges = {edge["key"] for edge in graph["edges"]}
    assert {f"{node['id']}->synthesis" for node in lanes} <= edges
    assert {f"research_gate->{node['id']}" for node in lanes} <= edges
    assert "research_gate->synthesis" not in edges

    counts = route_counts(
        graph,
        [
            "requirements",
            "architecture",
            "technical",
            "synthesis",
            "research_gate",
            "technical",
            "synthesis",
            "research_gate",
        ],
    )
    assert counts["research_gate->requirements"] == 0
    assert counts["research_gate->architecture"] == 0
    assert counts["research_gate->technical"] == 1


def test_direct_selective_gate_has_contract_forward_and_retry_edges() -> None:
    phases = [
        PhaseDef(
            id="requirements",
            type="producer",
            parallel_group="research",
            produces_contract="quill.research.requirements/v1",
        ),
        PhaseDef(
            id="technical",
            type="producer",
            parallel_group="research",
            produces_contract="quill.research.technical/v1",
        ),
        PhaseDef(
            id="research_gate",
            type="reviewer",
            against=("requirements", "technical"),
            gates=True,
            selective_on_block=("requirements", "technical"),
            produces_contract="quill.review.findings/v1",
        ),
    ]

    edges = {edge["key"]: edge for edge in build_phase_graph(phases)["edges"]}

    assert edges["requirements->research_gate"]["contracts"] == ["quill.research.requirements/v1"]
    assert edges["technical->research_gate"]["contracts"] == ["quill.research.technical/v1"]
    assert edges["research_gate->requirements"]["kinds"] == ["retry"]
    assert edges["research_gate->technical"]["kinds"] == ["retry"]


def test_data_dependencies_do_not_draw_shortcuts_across_intermediate_gate() -> None:
    phases = [
        PhaseDef(
            id="requirements",
            type="producer",
            parallel_group="research",
            produces_contract="quill.research.requirements/v1",
        ),
        PhaseDef(
            id="technical",
            type="producer",
            parallel_group="research",
            produces_contract="quill.research.technical/v1",
        ),
        PhaseDef(
            id="research_gate",
            type="reviewer",
            against=("requirements", "technical"),
            produces_contract="quill.review.findings/v1",
        ),
        PhaseDef(
            id="plan",
            type="producer",
            inputs=("requirements", "technical"),
            requires=("research_gate",),
            produces_contract="quill.plan/v1",
        ),
    ]

    edges = {edge["key"]: edge for edge in build_phase_graph(phases)["edges"]}

    assert set(edges) == {
        "requirements->research_gate",
        "technical->research_gate",
        "research_gate->plan",
    }
    assert edges["requirements->research_gate"]["contracts"] == ["quill.research.requirements/v1"]
    assert edges["technical->research_gate"]["contracts"] == ["quill.research.technical/v1"]
    assert edges["research_gate->plan"]["contracts"] == ["quill.review.findings/v1"]


def test_historical_graph_infers_observed_legacy_routes_and_last_phase(tmp_path: Path) -> None:
    events = [
        {"type": "run_plan", "summary": "legacy plan"},
        {"type": "phase_started", "phase": "impl", "label": "Implement", "phase_type": "producer"},
        {"type": "phase_done", "phase": "impl", "duration_s": 1.2},
        {"type": "phase_started", "phase": "commit", "label": "Commit", "phase_type": "producer"},
        {"type": "phase_started", "phase": "ci", "label": "CI", "phase_type": "mechanical"},
        {"type": "phase_started", "phase": "impl", "label": "Implement", "phase_type": "producer"},
        {"type": "phase_done", "phase": "impl", "duration_s": 2.1},
        {"type": "phase_started", "phase": "commit", "label": "Commit", "phase_type": "producer"},
        {"type": "phase_started", "phase": "ci", "label": "CI", "phase_type": "mechanical"},
    ]
    (tmp_path / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    graph, counts, durations, phase, label = _historical_graph(tmp_path)

    assert graph is not None
    assert [node.id for node in graph.nodes] == ["impl", "commit", "ci"]
    assert counts == {"impl->commit": 2, "commit->ci": 2, "ci->impl": 1}
    assert durations == {"impl": 3.3}
    assert next(edge for edge in graph.edges if edge.key == "ci->impl").kinds == ["retry"]
    assert (phase, label) == ("ci", "CI")


def test_historical_graph_does_not_recast_local_lane_retry_as_gate_retry(
    tmp_path: Path,
) -> None:
    phases = [
        PhaseDef(id="requirements", type="producer", parallel_group="research"),
        PhaseDef(id="technical", type="producer", parallel_group="research"),
        PhaseDef(
            id="research_gate",
            type="reviewer",
            gates=True,
            selective_on_block=("requirements", "technical"),
        ),
    ]
    durable_events = [
        events.run_plan("plan", phase_graph=build_phase_graph(phases)),
        events.phase_started("requirements", "Requirements"),
        events.phase_done("requirements", "Requirements"),
        events.phase_started("technical", "Technical"),
        events.phase_done("technical", "Technical"),
        events.retry("requirements", 1, 1, scope="phase", reason="malformed receipt"),
        events.phase_started("requirements", "Requirements"),
        events.phase_done("requirements", "Requirements"),
        events.phase_started("research_gate", "Research gate"),
    ]
    (tmp_path / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in durable_events) + "\n",
        encoding="utf-8",
    )

    _, counts, _, _, _ = _historical_graph(tmp_path)

    assert counts["research_gate->requirements"] == 0
    assert counts["research_gate->technical"] == 0


def test_historical_model_loads_require_a_durable_completion(tmp_path: Path) -> None:
    events = [
        {
            "type": "model_loading",
            "ts": 1.0,
            "phase": "plan",
            "label": "write plan",
            "model": "qwen",
        },
        {
            "type": "model_load_done",
            "ts": 21.0,
            "phase": "plan",
            "model": "qwen",
            "duration_s": 20.0,
            "success": False,
            "reason": "service stopped",
        },
        {"type": "model_loading", "ts": 22.0, "phase": "review", "model": "gemma"},
    ]
    (tmp_path / "state.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    loads = _historical_model_loads(tmp_path)

    assert len(loads) == 1
    assert loads[0].status == "failed"
    assert loads[0].duration_s == 20.0
    assert loads[0].reason == "service stopped"


def test_graph_names_each_concurrency_group() -> None:
    """The UI draws lanes inside a labelled container, so the graph must name the cluster."""
    phases = [
        PhaseDef(id="a", type="producer", label="lane a", parallel_group="research"),
        PhaseDef(id="b", type="producer", label="lane b", parallel_group="research"),
        PhaseDef(
            id="review_impl",
            type="reviewer",
            label="implementation audits",
            audits=(
                AuditDef("architecture", "Architecture", "arch.md", "m"),
                AuditDef("tests", "Tests", "tests.md", "m"),
            ),
        ),
        PhaseDef(id="solo", type="producer", label="solo"),
    ]

    groups = build_phase_graph(phases)["groups"]

    assert [(g["id"], g["label"]) for g in groups] == [
        # A parallel group's name is already human-readable; an audit cluster borrows its
        # parent phase's label rather than exposing the phase id.
        ("research", "research"),
        ("review_impl", "implementation audits"),
    ]
    assert groups[1]["members"] == ["review_impl.architecture", "review_impl.tests"]
    assert all(group["id"] != "solo" for group in groups)
