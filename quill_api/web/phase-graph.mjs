const NODE_HEIGHT = 64;
const X_GAP = 27;
const PAD = 28;
const PRIMARY_Y = 30;
// Retry lanes sit as close under the drawn content as they can. A second lane is only opened when
// two loops would otherwise draw on top of each other, so depth is paid for overlap, not for count.
const RETRY_LANE_CLEARANCE = 20;
const RETRY_LANE_GAP = 26;
// Retry routes read as a distinct layer, not as part of the forward flow: they meet a node between
// its centre line and its base rather than on the centre line, and their vertical legs sit nearer
// the node than a normal edge's mid-gap bend so the two never overlap.
const RETRY_ATTACH_RATIO = 0.75;
const RETRY_LEG_INSET = 9;
const PARALLEL_Y_GAP = 34;
// Self-check / self-fix decoration geometry. The vertical space the layout reserves is DERIVED
// from the shape actually drawn, so tightening the loop cannot silently clip it: change a value
// here and both the drawing (app.mjs, via selfLoopLayout) and the reservation move together.
const SELF_LOOP_INSET = 20;   // horizontal inset of the loop's vertical legs
const SELF_LOOP_REACH = 22;   // how far the loop travels from the node edge
const SELF_BADGE_HEIGHT = 16;
const SELF_BADGE_CHARACTER_WIDTH = 5.6;
const SELF_BADGE_INLINE_PADDING = 14;
const SELF_BADGE_CLEARANCE = 3;
// Text rows inside a node: duration and run count share the top row, tokens sit on the bottom.
const NODE_ROW_INSET = 15;
const NODE_BOTTOM_INSET = 8;
const NODE_GUTTER_X = 9;
const SELF_DECORATION_EXTENT =
  SELF_LOOP_REACH + SELF_BADGE_HEIGHT / 2 + SELF_BADGE_CLEARANCE;
const NODE_INLINE_PADDING = 16;
const GROUP_PAD_Y = 13;
// The container is inset further horizontally than vertically: routes fan out *inside* it, so the
// lanes need room between the dashed border and the blocks themselves.
const GROUP_PAD_X = 22;
// Where a fan-in trunk splits, measured from the container's left edge — inside the dashed border.
const GROUP_TRUNK_INSET = 11;
const GROUP_LABEL_HEIGHT = 19;
// Grouped lanes form a bottom-anchored staircase. Each lane above the bottom one moves back by one
// quarter of the shared node width. Retry loops therefore reach the bottom lane first, then fan
// outward toward the lanes above it without crossing.
const GROUP_LANE_X_STEP_RATIO = 0.25;
const PHASE_CHARACTER_WIDTH = 7.3;
const TOKEN_CHARACTER_WIDTH = 6.1;
const MAX_PHASE_TOKEN_LABEL = "9,999,999 tokens";

export function normalizePhaseGraph(run) {
  const graph = run?.phase_graph;
  if (!graph || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return null;
  const rawNodes = graph.nodes
    .filter((node) => node && typeof node.id === "string" && typeof node.label === "string")
    .map((node, index) => ({
      id: node.id,
      label: node.label,
      type: typeof node.type === "string" ? node.type : "phase",
      order: Number.isInteger(node.order) ? node.order : index,
      column: Number.isInteger(node.column) ? node.column : null,
      lane: Number.isInteger(node.lane) ? node.lane : null,
      group: typeof node.group === "string" ? node.group : null,
      selfCheck: node.self_check === true,
      selfFix: node.self_fix === true,
      selfFixStatus: ["active", "completed", "failed"].includes(run.self_fixes?.[node.id])
        ? run.self_fixes[node.id]
        : null,
      selfFixRan: node.self_fix === true
        && ["active", "completed", "failed"].includes(run.self_fixes?.[node.id]),
      executionCount: Math.max(0, Number(run.phase_execution_counts?.[node.id]) || 0),
      totalTokens: Math.max(0, Number(run.phase_token_counts?.[node.id]) || 0),
      durationSeconds: Number.isFinite(Number(run.phase_durations?.[node.id]))
        ? Math.ceil(Math.max(0, Number(run.phase_durations[node.id])))
        : null,
      contractState: normalizeContractState(run.contract_states?.[node.id]),
    }))
    .sort((left, right) => left.order - right.order);
  const dottedGroups = new Map();
  for (const node of rawNodes) {
    const splitAt = node.id.lastIndexOf(".");
    if (splitAt <= 0) continue;
    const prefix = node.id.slice(0, splitAt);
    dottedGroups.set(prefix, (dottedGroups.get(prefix) || 0) + 1);
  }
  for (const node of rawNodes) {
    if (node.group) continue;
    const splitAt = node.id.lastIndexOf(".");
    const prefix = splitAt > 0 ? node.id.slice(0, splitAt) : null;
    if (prefix && dottedGroups.get(prefix) > 1) node.group = prefix;
  }
  const hasCompleteLayout = rawNodes.every((node) => node.column !== null && node.lane !== null);
  if (!hasCompleteLayout) {
    const groupLanes = new Map();
    const stageColumns = new Map();
    let nextColumn = 0;
    for (const node of rawNodes) {
      const stage = node.group || node.id;
      if (!stageColumns.has(stage)) stageColumns.set(stage, nextColumn++);
      node.column = stageColumns.get(stage);
      const lane = groupLanes.get(stage) || 0;
      node.lane = node.group ? lane : 0;
      groupLanes.set(stage, lane + 1);
    }
  }
  const nodes = rawNodes.map((node) => ({ ...node, displayId: laneDisplayId(node) }));
  if (!nodes.length) return { nodes: [], edges: [], groups: [] };
  const ids = new Set(nodes.map((node) => node.id));
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const seen = new Set();
  const edges = [];
  for (const edge of graph.edges) {
    if (!edge || !ids.has(edge.source) || !ids.has(edge.target)) continue;
    const key = typeof edge.key === "string" ? edge.key : `${edge.source}->${edge.target}`;
    if (seen.has(key)) continue;
    const sourceNode = nodesById.get(edge.source);
    const targetNode = nodesById.get(edge.target);
    const kinds = Array.isArray(edge.kinds)
      ? edge.kinds.filter((kind) => ["normal", "retry"].includes(kind))
      : [];
    const visibleKinds = kinds.filter((kind) => (
      kind === "retry" || targetNode.column === sourceNode.column + 1
    ));
    // Older run plans included data dependencies as normal graph routes. A dependency that jumps
    // over an intermediate column is not executable flow and can visually pass through its gate.
    if (!visibleKinds.length) continue;
    seen.add(key);
    edges.push({
      key,
      source: edge.source,
      target: edge.target,
      kinds: visibleKinds,
      contracts: Array.isArray(edge.contracts)
        ? edge.contracts.filter((contract) => typeof contract === "string" && contract.length > 0)
        : [],
      count: Math.max(0, Number(run.phase_route_counts?.[key]) || 0),
    });
  }
  return {
    ...injectModelLoadNodes(nodes, edges, run?.model_loads),
    groups: concurrencyGroups(nodes, graph.groups),
  };
}

export function normalizeContractState(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const attempt = Number(value.attempt);
  const state = typeof value.state === "string" ? value.state : "";
  if (!Number.isInteger(attempt) || attempt < 1 || !state) return null;
  return {
    attempt,
    state,
    kind: typeof value.kind === "string" ? value.kind : "",
    status: typeof value.status === "string" ? value.status : "",
    digest: typeof value.digest === "string" ? value.digest : "",
  };
}

/** Identify graph changes that require new geometry.
 *
 * Traversal counts, durations, and token totals intentionally do not participate. Contract attempt
 * and state do because they control a visible node badge that must repaint on lifecycle changes.
 * A retry route becoming visible does participate because zero-count retry routes are not drawn.
 */
export function phaseGraphStructureSignature(graph) {
  if (!graph) return "empty";
  return JSON.stringify({
    nodes: (graph.nodes || []).map((node) => [
      node.id,
      node.type,
      node.group,
      node.column,
      node.lane,
      node.displayId,
      node.modelName || "",
      Boolean(node.selfCheck),
      Boolean(node.selfFixRan),
      node.contractState?.attempt || 0,
      node.contractState?.state || "",
    ]),
    edges: (graph.edges || [])
      .filter((edge) => edge.count > 0 || edge.kinds.includes("normal"))
      .map((edge) => [edge.key, edge.source, edge.target, [...edge.kinds].sort()]),
    groups: (graph.groups || []).map((group) => [group.id, group.label, [...group.members]]),
  });
}

/** Cluster lanes by `node.group`, preferring the backend's human label when the run carries one.
 *
 * Membership is derived rather than read from `graph.groups` so a run whose stored plan predates
 * group metadata still renders containers — the plan is captured once at run start and never
 * rewritten, so an in-flight run would otherwise show nothing until it was restarted.
 */
export function concurrencyGroups(nodes = [], declared = []) {
  const labels = new Map((Array.isArray(declared) ? declared : [])
    .filter((group) => group && typeof group.id === "string")
    .map((group) => [
      group.id,
      typeof group.label === "string" && group.label ? group.label : group.id,
    ]));
  const members = new Map();
  for (const node of nodes) {
    if (!node.group) continue;
    members.set(node.group, [...(members.get(node.group) || []), node.id]);
  }
  return [...members.entries()]
    .filter(([, ids]) => ids.length > 1)
    .map(([id, ids]) => ({ id, label: labels.get(id) || id, members: ids }));
}

/** A lane inside a named group shows only its own name; the container carries the shared prefix. */
export function laneDisplayId(node) {
  if (!node.group) return node.id;
  for (const separator of [".", "_"]) {
    const prefix = `${node.group}${separator}`;
    if (node.id.startsWith(prefix) && node.id.length > prefix.length) {
      return node.id.slice(prefix.length);
    }
  }
  return node.id;
}

export function injectModelLoadNodes(nodes, edges, modelLoads = []) {
  const observed = Array.isArray(modelLoads)
    ? modelLoads.filter((load) => load && typeof load.phase === "string" && typeof load.model === "string")
    : [];
  if (!observed.length) return { nodes, edges };

  const aggregates = new Map();
  for (const [index, load] of observed.entries()) {
    const target = nodes.find((node) => node.id === load.phase)
      || nodes.find((node) => node.group === load.phase);
    if (!target) continue;
    const stage = target.group || target.id;
    const key = `${stage}\n${load.model}`;
    const current = aggregates.get(key) || {
      key,
      stage,
      model: load.model,
      targetColumn: target.column,
      targetIds: nodes
        .filter((node) => node.column === target.column && (target.group ? node.group === target.group : node.id === target.id))
        .map((node) => node.id),
      firstIndex: index,
      terminalCount: 0,
      durationSeconds: 0,
      latestStatus: "active",
      activeStartedAt: null,
      reason: null,
    };
    if (["completed", "failed"].includes(load.status)) {
      current.terminalCount += 1;
      current.durationSeconds += Math.max(0, Number(load.duration_s) || 0);
    }
    current.latestStatus = ["active", "completed", "failed"].includes(load.status)
      ? load.status
      : current.latestStatus;
    current.activeStartedAt = load.status === "active" ? Number(load.started_at) || null : null;
    current.reason = typeof load.reason === "string" ? load.reason : null;
    aggregates.set(key, current);
  }
  const loads = [...aggregates.values()].sort((left, right) => left.firstIndex - right.firstIndex);
  if (!loads.length) return { nodes, edges };

  const byColumn = new Map();
  for (const load of loads) {
    const columnLoads = byColumn.get(load.targetColumn) || [];
    columnLoads.push(load);
    byColumn.set(load.targetColumn, columnLoads);
  }
  const columns = [...new Set(nodes.map((node) => node.column))].sort((left, right) => left - right);
  const remappedColumns = new Map();
  let nextColumn = 0;
  for (const column of columns) {
    for (const load of byColumn.get(column) || []) load.column = nextColumn++;
    remappedColumns.set(column, nextColumn++);
  }
  const remappedNodes = nodes.map((node) => ({ ...node, column: remappedColumns.get(node.column) }));
  const loadNodes = loads.map((load, index) => ({
    id: `__model_load__${index + 1}`,
    label: `Load ${load.model}`,
    displayId: "model load",
    modelName: load.model,
    type: "model_load",
    order: -loads.length + index,
    column: load.column,
    lane: 0,
    group: null,
    selfCheck: false,
    selfFix: false,
    selfFixStatus: null,
    selfFixRan: false,
    executionCount: load.terminalCount,
    totalTokens: 0,
    durationSeconds: load.durationSeconds || (load.latestStatus === "active" ? null : 0),
    loadStatus: load.latestStatus,
    loadStartedAt: load.activeStartedAt,
    reason: load.reason,
    targetIds: load.targetIds,
    stage: load.stage,
    detachedGapAfter: false,
  }));

  let rewritten = edges.map((edge) => ({ ...edge, kinds: [...edge.kinds] }));
  const stages = [...new Set(loadNodes.map((node) => node.stage))];
  for (const stage of stages) {
    const stageLoads = loadNodes.filter((node) => node.stage === stage);
    const targetIds = new Set(stageLoads[0].targetIds);
    const firstLoad = stageLoads[0];
    const hasNormalInbound = rewritten.some((edge) => (
      edge.kinds.includes("normal")
      && targetIds.has(edge.target)
      && !targetIds.has(edge.source)
    ));
    if (!hasNormalInbound) stageLoads.at(-1).detachedGapAfter = true;
    const redirected = [];
    for (const edge of rewritten) {
      const carriesNormal = edge.kinds.includes("normal") && targetIds.has(edge.target);
      if (!carriesNormal) {
        redirected.push(edge);
        continue;
      }
      if (edge.kinds.includes("retry")) {
        redirected.push({ ...edge, kinds: ["retry"] });
      }
      redirected.push({
        ...edge,
        key: `${edge.source}->${firstLoad.id}`,
        target: firstLoad.id,
        kinds: ["normal"],
      });
    }
    for (let index = 0; index < stageLoads.length - 1; index += 1) {
      redirected.push({
        key: `${stageLoads[index].id}->${stageLoads[index + 1].id}`,
        source: stageLoads[index].id,
        target: stageLoads[index + 1].id,
        kinds: ["normal"],
        count: stageLoads[index].executionCount,
      });
    }
    if (hasNormalInbound) {
      const lastLoad = stageLoads.at(-1);
      for (const targetId of targetIds) {
        redirected.push({
          key: `${lastLoad.id}->${targetId}`,
          source: lastLoad.id,
          target: targetId,
          kinds: ["normal"],
          count: lastLoad.executionCount,
        });
      }
    }
    rewritten = redirected;
  }

  const seen = new Set();
  const uniqueEdges = rewritten.filter((edge) => {
    const key = `${edge.source}\n${edge.target}\n${edge.kinds.join(",")}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return { nodes: [...remappedNodes, ...loadNodes], edges: uniqueEdges };
}

/** Path, badge box, and caption anchor for one self-check (above) or self-fix (below) loop.
 *
 * Sole source of the decoration's shape. `SELF_DECORATION_EXTENT` is computed from the same
 * constants, so the reserved space always covers what this returns.
 */
/** Baselines for the text rows inside a node, so callers never hardcode them. */
export function nodeTextRows(nodeHeight) {
  return { topY: NODE_ROW_INSET, bottomY: nodeHeight - NODE_BOTTOM_INSET, gutterX: NODE_GUTTER_X };
}

export function selfLoopLayout(label, nodeWidth, nodeHeight, { below = false } = {}) {
  const edge = below ? nodeHeight : 0;
  const direction = below ? 1 : -1;
  const apex = edge + direction * SELF_LOOP_REACH;
  const badgeWidth = Math.ceil(
    String(label).length * SELF_BADGE_CHARACTER_WIDTH + SELF_BADGE_INLINE_PADDING,
  );
  return {
    // Out of the node edge, along to the far leg, then back to the edge for the arrowhead.
    path: `M ${nodeWidth - SELF_LOOP_INSET} ${edge} V ${apex} H ${SELF_LOOP_INSET} V ${edge + direction * 2}`,
    badge: {
      x: (nodeWidth - badgeWidth) / 2,
      y: apex - SELF_BADGE_HEIGHT / 2,
      width: badgeWidth,
      height: SELF_BADGE_HEIGHT,
    },
    captionX: nodeWidth / 2,
    captionY: apex,
    extent: SELF_DECORATION_EXTENT,
  };
}

export function formatPhaseDuration(seconds) {
  const wholeSeconds = Math.ceil(Math.max(0, Number(seconds) || 0));
  const minutes = Math.floor(wholeSeconds / 60);
  return `${minutes}m${String(wholeSeconds % 60).padStart(2, "0")}s`;
}

export function phaseNodeWidth(nodes = []) {
  const titleWidth = Math.max(
    0,
    ...nodes.map((node) => Math.max(
      String(node.displayId || node.id || "").length * PHASE_CHARACTER_WIDTH,
      String(node.modelName || "").length * TOKEN_CHARACTER_WIDTH,
    )),
  );
  const tokenWidth = MAX_PHASE_TOKEN_LABEL.length * TOKEN_CHARACTER_WIDTH;
  return Math.ceil(Math.max(titleWidth, tokenWidth) + NODE_INLINE_PADDING);
}

export function phaseGraphMetrics(executions = [], liveUsage = {}) {
  const executionCounts = {};
  const tokenCounts = {};
  for (const execution of executions || []) {
    const phase = execution?.phase;
    if (typeof phase !== "string") continue;
    if (execution.verdict != null) {
      executionCounts[phase] = (executionCounts[phase] || 0) + 1;
    }
    tokenCounts[phase] = (tokenCounts[phase] || 0)
      + Math.max(0, Number(execution.context_window_tokens ?? execution.total_tokens) || 0);
  }
  for (const [phase, usage] of Object.entries(liveUsage?.phase_usages || {})) {
    tokenCounts[phase] = Math.max(
      0,
      Number(usage?.context_window_tokens ?? usage?.total_tokens) || 0,
    );
  }
  return { executionCounts, tokenCounts };
}

export function phaseNodeStates(run, graph) {
  const tracksActivePhases = run?.active_phases != null
    && typeof run.active_phases === "object"
    && !Array.isArray(run.active_phases);
  const visited = new Set();
  const phasesWaitingForModel = new Set(
    graph.nodes
      .filter((node) => node.type === "model_load" && node.loadStatus === "active")
      .flatMap((node) => node.targetIds || []),
  );
  const latestVerdicts = new Map();
  for (const entry of run?.history || []) {
    if (typeof entry?.phase === "string" && typeof entry?.verdict === "string") {
      latestVerdicts.set(entry.phase, entry.verdict.toUpperCase());
    }
  }
  for (const edge of graph.edges) {
    if (edge.count > 0) {
      visited.add(edge.source);
      visited.add(edge.target);
    }
  }
  if (run?.phase && !phasesWaitingForModel.has(run.phase)) visited.add(run.phase);
  for (const phase of Object.keys(run?.active_phases || {})) visited.add(phase);
  for (const node of graph.nodes) if (node.durationSeconds !== null) visited.add(node.id);
  if (run?.status === "done") for (const node of graph.nodes) visited.add(node.id);
  return Object.fromEntries(graph.nodes.map((node) => {
    if (node.type === "model_load") return [node.id, node.loadStatus || "unvisited"];
    const active = Object.hasOwn(run?.active_phases || {}, node.id)
      || (!tracksActivePhases
        && ["running", "needs_decision"].includes(run?.status)
        && node.id === run?.phase);
    if (active) return [node.id, "active"];
    const latestVerdict = latestVerdicts.get(node.id);
    if (["BLOCK", "FAILED", "CRASH", "GARBAGE"].includes(latestVerdict)) {
      return [node.id, "failed"];
    }
    if (["DONE", "PASS"].includes(latestVerdict)) return [node.id, "completed"];
    if (["failed", "halted"].includes(run?.status) && node.id === run?.phase) return [node.id, "failed"];
    return [node.id, visited.has(node.id) ? "completed" : "unvisited"];
  }));
}

/** Roll a container's lanes up into one state, using the same vocabulary as a node.
 *
 * Active wins while anything is still running — the group has not settled yet — then a failure,
 * then completion. A container is only green once every lane inside it succeeded.
 */
export function groupState(group, nodes, states) {
  const members = nodes.filter((node) => node.group === group.id);
  if (!members.length) return "unvisited";
  const lane = members.map((node) => states[node.id] || "unvisited");
  if (lane.includes("active")) return "active";
  if (lane.includes("failed")) return "failed";
  return lane.every((state) => state === "completed") ? "completed" : "unvisited";
}

export function phaseEdgeState(edge, states) {
  const retryOnly = edge.kinds.includes("retry") && !edge.kinds.includes("normal");
  return states[retryOnly ? edge.target : edge.source] || "unvisited";
}

export function centeredPhaseScroll(centerX, viewportWidth, contentWidth) {
  const maximum = Math.max(0, Number(contentWidth) - Number(viewportWidth));
  const target = Number(centerX) - Number(viewportWidth) / 2;
  return Math.min(maximum, Math.max(0, Number.isFinite(target) ? target : 0));
}

export function layoutPhaseGraph(graph) {
  if (!graph.nodes.length) {
    return { nodes: [], edges: [], groups: [], width: 360, height: PAD * 2 };
  }
  const columnCount = Math.max(...graph.nodes.map((node) => node.column), 0) + 1;
  const nodeWidth = phaseNodeWidth(graph.nodes);
  const stages = new Map();
  for (const node of graph.nodes) {
    const stage = node.group || node.id;
    const members = stages.get(stage) || [];
    members.push(node);
    stages.set(stage, members);
  }
  const stageLayouts = [];
  for (const members of stages.values()) {
    members.sort((left, right) => left.lane - right.lane);
    const relativeY = new Map();
    const relativeX = new Map();
    let previous = null;
    for (const [index, node] of members.entries()) {
      const topExtent = node.selfCheck ? SELF_DECORATION_EXTENT : 0;
      const bottomExtent = node.selfFixRan ? SELF_DECORATION_EXTENT : 0;
      const y = previous === null
        ? 0
        : previous.y
          + NODE_HEIGHT
          + previous.bottomExtent
          + PARALLEL_Y_GAP
          + topExtent;
      relativeY.set(node.id, y);
      relativeX.set(
        node.id,
        node.group ? index * nodeWidth * GROUP_LANE_X_STEP_RATIO : 0,
      );
      previous = { y, bottomExtent };
    }
    const middle = Math.floor(members.length / 2);
    const centerOffset = members.length % 2
      ? relativeY.get(members[middle].id)
      : (relativeY.get(members[middle - 1].id) + relativeY.get(members[middle].id)) / 2;
    stageLayouts.push({ members, relativeX, relativeY, centerOffset });
  }
  const centerY = Math.max(PRIMARY_Y, ...stageLayouts.flatMap((stage) => (
    stage.members.map((node) => {
      // A grouped lane that also draws a self-check needs room for BOTH: the container caption
      // sits above the decoration, so the extents stack rather than compete.
      const minimumY = Math.max(
        PRIMARY_Y,
        PAD
        + (node.selfCheck ? SELF_DECORATION_EXTENT : 0)
        + (node.group ? GROUP_PAD_Y + GROUP_LABEL_HEIGHT : 0),
      );
      return minimumY - stage.relativeY.get(node.id) + stage.centerOffset;
    })
  )));
  const nodeY = Object.fromEntries(stageLayouts.flatMap((stage) => (
    stage.members.map((node) => [
      node.id,
      centerY + stage.relativeY.get(node.id) - stage.centerOffset,
    ])
  )));
  const nodeOffsetX = Object.fromEntries(stageLayouts.flatMap((stage) => (
    stage.members.map((node) => [node.id, stage.relativeX.get(node.id)])
  )));
  const columnWidths = Array.from({ length: columnCount }, (_, column) => Math.max(
    nodeWidth,
    ...graph.nodes.filter((node) => node.column === column)
      .map((node) => nodeWidth + nodeOffsetX[node.id]),
  ));
  const columnX = [];
  const detachedGapColumns = new Set(
    graph.nodes.filter((node) => node.detachedGapAfter).map((node) => node.column),
  );
  // A container is inset GROUP_PAD_X beyond its lanes on each side. Without extra room its border
  // would eat the gap to the neighbouring column and crowd the arrowheads there.
  const groupedColumns = new Set(
    graph.nodes.filter((node) => node.group).map((node) => node.column),
  );
  let nextX = PAD;
  for (const [column, width] of columnWidths.entries()) {
    if (groupedColumns.has(column)) nextX += GROUP_PAD_X;
    columnX.push(nextX);
    nextX += width + X_GAP + (detachedGapColumns.has(column) ? nodeWidth : 0);
    if (groupedColumns.has(column)) nextX += GROUP_PAD_X;
  }
  const positions = Object.fromEntries(graph.nodes.map((node) => [node.id, {
    x: columnX[node.column] + nodeOffsetX[node.id],
    y: nodeY[node.id],
    width: nodeWidth,
    height: NODE_HEIGHT,
  }]));
  const contentBottom = Math.max(...graph.nodes.map((node) => (
    positions[node.id].y
    + NODE_HEIGHT
    + (node.selfFixRan ? SELF_DECORATION_EXTENT : 0)
    + (node.group ? GROUP_PAD_Y : 0)
  )));
  const positionedNodes = graph.nodes.map((node) => ({ ...node, ...positions[node.id] }));
  const boxes = groupBoxes(graph.groups, positionedNodes);
  const entryTrunk = new Map(boxes.map((box) => [box.id, box.trunkX]));
  const exitTrunk = new Map(boxes.map((box) => [box.id, box.exitTrunkX]));
  const visibleEdges = graph.edges.filter((edge) => (
    edge.count > 0 || edge.kinds.includes("normal")
  ));
  const nodeGroup = new Map(graph.nodes.map((node) => [node.id, node.group]));
  const isRetry = (edge) => edge.kinds.includes("retry") && !edge.kinds.includes("normal");
  // Lanes are assigned from the routes' horizontal spans before any path is built, so a loop knows
  // its depth from what else is drawn rather than from its position in the edge list.
  const lanes = assignRetryLanes(visibleEdges.filter(isRetry).map((edge) => ({
    key: edge.key,
    ...retrySpan(positions[edge.source], positions[edge.target]),
  })));
  const edges = visibleEdges.map((edge) => {
    // Retry routes no longer meet a node on its centre line, so a normal edge and an opposing
    // retry can each keep their own arrowhead without the two colliding at the node edge.
    const lane = isRetry(edge) ? lanes.get(edge.key) + 1 : 0;
    return {
      ...edge,
      lane,
      showArrow: true,
      path: orthogonalPath(
        positions[edge.source],
        positions[edge.target],
        lane,
        lane ? retryLaneY(contentBottom, lane - 1) : null,
        // Leaving a container takes priority over entering one: the bend belongs next to the
        // block the route departs, so an outbound line turns as close in as an inbound one.
        lane
          ? null
          : exitTrunk.get(nodeGroup.get(edge.source))
            ?? entryTrunk.get(nodeGroup.get(edge.target))
            ?? null,
      ),
    };
  });
  const deepestLane = Math.max(0, ...edges.map((edge) => edge.lane));
  const width = Math.max(360, nextX - X_GAP + PAD);
  const height = deepestLane
    ? retryLaneY(contentBottom, deepestLane - 1) + PAD
    : contentBottom + PAD;
  return { nodes: positionedNodes, edges, groups: boxes, width, height, contentBottom };
}

/** Bounding container for each concurrency group, drawn behind its lanes. */
export function groupBoxes(groups = [], nodes = []) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  return (groups || []).flatMap((group) => {
    const members = (group.members || []).map((id) => byId.get(id)).filter(Boolean);
    if (members.length < 2) return [];
    const left = Math.min(...members.map((node) => node.x)) - GROUP_PAD_X;
    const right = Math.max(...members.map((node) => node.x + node.width)) + GROUP_PAD_X;
    const top = Math.min(...members.map((node) => (
      node.y - (node.selfCheck ? SELF_DECORATION_EXTENT : 0)
    ))) - GROUP_PAD_Y - GROUP_LABEL_HEIGHT;
    const bottom = Math.max(...members.map((node) => (
      node.y + node.height + (node.selfFixRan ? SELF_DECORATION_EXTENT : 0)
    ))) + GROUP_PAD_Y;
    return [{
      id: group.id,
      label: group.label || group.id,
      memberCount: members.length,
      x: left,
      y: top,
      width: right - left,
      height: bottom - top,
      labelX: left + GROUP_PAD_X,
      labelY: top + GROUP_LABEL_HEIGHT - 5,
      // Routes converge on these trunks and fan out inside the border, rather than each crossing
      // it separately. Symmetric, so a route leaves as close to its block as one arrives.
      trunkX: left + GROUP_TRUNK_INSET,
      exitTrunkX: right - GROUP_TRUNK_INSET,
    }];
  });
}

export function orthogonalPath(source, target, lane = 0, laneY = null, trunkX = null) {
  const startX = source.x + source.width;
  const endX = target.x;
  if (!lane) {
    const startY = source.y + source.height / 2;
    const endY = target.y + target.height / 2;
    if (endX >= startX) {
      if (startY === endY) return `M ${startX} ${startY} H ${endX}`;
      // A trunk bends inside the target's container so several routes share one entry line and
      // fan out behind the border; otherwise bend midway across the gap.
      const bendX = trunkX === null ? (startX + endX) / 2 : trunkX;
      return `M ${startX} ${startY} H ${bendX} V ${endY} H ${endX}`;
    }
  }
  const depth = laneY === null ? retryLaneY(target.y + target.height) : laneY;
  const startY = source.y + source.height * RETRY_ATTACH_RATIO;
  const endY = target.y + target.height * RETRY_ATTACH_RATIO;
  const exitX = startX + RETRY_LEG_INSET;
  const enterX = endX - RETRY_LEG_INSET;
  return `M ${startX} ${startY} H ${exitX} V ${depth} H ${enterX} V ${endY} H ${endX}`;
}

/** Depth of retry lane ``index`` (0-based), measured from the deepest drawn content. */
export function retryLaneY(contentBottom, index = 0) {
  return contentBottom + RETRY_LANE_CLEARANCE + index * RETRY_LANE_GAP;
}

/** Horizontal span a retry route occupies on its lane. */
export function retrySpan(source, target) {
  const exitX = source.x + source.width + RETRY_LEG_INSET;
  const enterX = target.x - RETRY_LEG_INSET;
  return { lo: Math.min(exitX, enterX), hi: Math.max(exitX, enterX) };
}

/** Assign each retry route the shallowest lane on which nothing else is already drawn.
 *
 * Shorter loops are placed first, so they settle nearest the phase blocks and a long route -- one
 * reaching back across many columns, like a research gate returning to its lanes -- is pushed
 * outward rather than cutting under the short ones at the same depth. Routes that do not overlap
 * share a lane, so depth is only spent where two loops genuinely collide.
 */
export function assignRetryLanes(spans) {
  const ordered = [...spans].sort((left, right) => (
    (left.hi - left.lo) - (right.hi - right.lo) || left.lo - right.lo
  ));
  const lanes = [];
  const assigned = new Map();
  for (const span of ordered) {
    let index = lanes.findIndex((lane) => (
      lane.every((taken) => span.hi <= taken.lo || taken.hi <= span.lo)
    ));
    if (index === -1) index = lanes.push([]) - 1;
    lanes[index].push(span);
    assigned.set(span.key, index);
  }
  return assigned;
}

/** Where a retry route meets a node: between its centre line and its base. */
export function retryAttachY(node) {
  return node.y + node.height * RETRY_ATTACH_RATIO;
}

export function edgeLabelPosition(edge, layout) {
  const source = layout.nodes.find((node) => node.id === edge.source);
  const target = layout.nodes.find((node) => node.id === edge.target);
  if (!edge.lane) return { x: (source.x + source.width + target.x) / 2, y: source.y + source.height / 2 - 9 };
  return {
    x: (source.x + source.width + target.x) / 2,
    y: retryLaneY(
      Math.max(layout.contentBottom || 0, PRIMARY_Y + NODE_HEIGHT),
      edge.lane - 1,
    ) - 7,
  };
}
