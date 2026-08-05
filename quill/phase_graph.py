"""Pure phase-topology and traversal projections shared by the driver and API."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from quill.config import PhaseDef


class PhaseGraphNode(TypedDict):
    id: str
    label: str
    type: str
    order: int
    column: NotRequired[int]
    lane: NotRequired[int]
    group: NotRequired[str | None]
    self_check: NotRequired[bool]
    self_fix: NotRequired[bool]


class PhaseGraphEdge(TypedDict):
    key: str
    source: str
    target: str
    kinds: list[Literal["normal", "retry"]]
    contracts: NotRequired[list[str]]


class PhaseGraphGroup(TypedDict):
    """One concurrency cluster, so the UI can draw its lanes inside a named container."""

    id: str
    label: str
    members: list[str]


class PhaseGraph(TypedDict):
    nodes: list[PhaseGraphNode]
    edges: list[PhaseGraphEdge]
    groups: NotRequired[list[PhaseGraphGroup]]


def build_phase_graph(phases: list[PhaseDef]) -> PhaseGraph:
    """Build the declared normal path and gated retry routes."""
    stages: list[list[str]] = []
    nodes: list[PhaseGraphNode] = []
    groups: list[PhaseGraphGroup] = []
    order = 0
    column = 0
    index = 0
    while index < len(phases):
        phase = phases[index]
        if phase.audits:
            stage = []
            groups.append(
                {
                    "id": phase.id,
                    "label": phase.label or phase.id,
                    "members": [f"{phase.id}.{audit.id}" for audit in phase.audits],
                }
            )
            for lane, audit in enumerate(phase.audits):
                node_id = f"{phase.id}.{audit.id}"
                stage.append(node_id)
                nodes.append(
                    {
                        "id": node_id,
                        "label": audit.label,
                        "type": "reviewer",
                        "order": order,
                        "column": column,
                        "lane": lane,
                        "group": phase.id,
                        "self_check": phase.self_check,
                        "self_fix": True,
                    }
                )
                order += 1
            stages.append(stage)
            index += 1
        elif phase.parallel_group is not None:
            group = phase.parallel_group
            members: list[PhaseDef] = []
            while index < len(phases) and phases[index].parallel_group == group:
                members.append(phases[index])
                index += 1
            groups.append(
                {"id": group, "label": group, "members": [member.id for member in members]}
            )
            stage = []
            for lane, member in enumerate(members):
                stage.append(member.id)
                nodes.append(
                    {
                        "id": member.id,
                        "label": member.label or member.id,
                        "type": member.type,
                        "order": order,
                        "column": column,
                        "lane": lane,
                        "group": group,
                        "self_check": member.self_check,
                        "self_fix": True,
                    }
                )
                order += 1
            stages.append(stage)
        else:
            nodes.append(
                {
                    "id": phase.id,
                    "label": phase.label or phase.id,
                    "type": phase.type,
                    "order": order,
                    "column": column,
                    "lane": 0,
                    "group": None,
                    "self_check": phase.self_check,
                    "self_fix": phase.type != "mechanical",
                }
            )
            stages.append([phase.id])
            order += 1
            index += 1
        column += 1
    edge_kinds: dict[tuple[str, str], set[Literal["normal", "retry"]]] = {}

    def add(source: str, target: str, kind: Literal["normal", "retry"]) -> None:
        edge_kinds.setdefault((source, target), set()).add(kind)

    for source_stage, target_stage in zip(stages, stages[1:], strict=False):
        for source in source_stage:
            for target in target_stage:
                add(source, target, "normal")
    by_id = {phase.id: phase for phase in phases}
    contract_edges: dict[tuple[str, str], set[str]] = {}
    for consumer in phases:
        dependencies = (
            *consumer.inputs,
            *consumer.synthesizes,
            *consumer.against,
            *consumer.reconciles,
            *consumer.requires,
        )
        for dependency in dependencies:
            producer = by_id.get(dependency)
            if producer is None:
                continue
            sources = (
                [f"{producer.id}.{audit.id}" for audit in producer.audits]
                if producer.audits
                else [producer.id]
            )
            for source in sources:
                add(source, consumer.id, "normal")
                if producer.produces_contract:
                    contract_edges.setdefault((source, consumer.id), set()).add(
                        producer.produces_contract
                    )
    for gate in phases:
        if not gate.gates or (not gate.on_block and not gate.selective_on_block):
            continue
        # ``on_block`` is one back-edge. After taking it, execution follows the ordinary forward
        # edges until it reaches the gate again.
        targets = gate.selective_on_block or gate.on_block
        for target in targets:
            add(gate.id, target, "retry")

    edges: list[PhaseGraphEdge] = []
    for edge_source, edge_target in sorted(edge_kinds, key=lambda pair: (pair[0], pair[1])):
        kinds = edge_kinds[(edge_source, edge_target)]
        edge: PhaseGraphEdge = {
                "key": f"{edge_source}->{edge_target}",
                "source": edge_source,
                "target": edge_target,
                "kinds": [kind for kind in ("normal", "retry") if kind in kinds],
            }
        contracts = sorted(contract_edges.get((edge_source, edge_target), ()))
        if contracts:
            edge["contracts"] = contracts
        edges.append(edge)
    return {"nodes": nodes, "edges": edges, "groups": groups}


def route_counts(graph: PhaseGraph | None, sequence: list[str]) -> dict[str, int]:
    """Count only transitions that correspond to a declared directed route."""
    if graph is None:
        return {}
    counts = {edge["key"]: 0 for edge in graph["edges"]}
    keys = {(edge["source"], edge["target"]): edge["key"] for edge in graph["edges"]}
    for source, target in zip(sequence, sequence[1:], strict=False):
        key = keys.get((source, target))
        if key is not None:
            counts[key] += 1
    nodes = {node["id"]: node for node in graph["nodes"]}
    occurrences = {node_id: sequence.count(node_id) for node_id in nodes}
    # Concurrent audit starts are serialized in the event log but are horizontal in the declared
    # topology. Count their fan-in/fan-out routes from lane occurrences rather than false adjacency.
    for edge in graph["edges"]:
        source_node = nodes.get(edge["source"])
        target_node = nodes.get(edge["target"])
        if source_node is None or target_node is None or "normal" not in edge["kinds"]:
            continue
        if target_node.get("group") is not None:
            counts[edge["key"]] = min(occurrences[edge["source"]], occurrences[edge["target"]])
        elif source_node.get("group") is not None:
            counts[edge["key"]] = min(occurrences[edge["source"]], occurrences[edge["target"]])
    for edge in graph["edges"]:
        target_node = nodes.get(edge["target"])
        if (
            target_node is not None
            and edge["kinds"] == ["retry"]
            and target_node.get("group") is not None
        ):
            # Each grouped lane runs once on the normal path. Later occurrences are selective
            # retries, even though concurrent event ordering cannot encode gate adjacency.
            counts[edge["key"]] = max(0, occurrences[edge["target"]] - 1)
    return counts
