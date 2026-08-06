import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  ApiError,
  QuillApi,
  apiFetch,
  buildQuery,
  errorMessage,
  relativePath,
  segment,
} from "../../quill_api/web/api.mjs";
import {
  branchName,
  canAnswerRun,
  canStopRun,
  chooseSelection,
  diagnosticSummary,
  formatBytes,
  formatDuration,
  formatMemoryGb,
  formatPercent,
  formatTemperature,
  liveRunLabel,
  parseRoute,
  preferredWorkType,
  pruneQueueSelection,
  queueCapableRepositories,
  groupSelectionState,
  runElapsed,
  safeExternalUrl,
  statusTone,
  temperatureTone,
  validCatalogName,
  validReason,
} from "../../quill_api/web/format.mjs";
import {
  centeredPhaseScroll,
  concurrencyGroups,
  formatPhaseDuration,
  groupBoxes,
  groupState,
  laneDisplayId,
  layoutPhaseGraph,
  nodeTextRows,
  normalizeContractState,
  normalizePhaseGraph,
  orthogonalPath,
  phaseEdgeState,
  phaseGraphMetrics,
  phaseGraphStructureSignature,
  phaseNodeStates,
  phaseNodeWidth,
  retryAttachY,
  retryLaneY,
  selfLoopLayout,
} from "../../quill_api/web/phase-graph.mjs";

test("phase graph preserves contract edges and latest validated attempt status", () => {
  const run = {
    phase_graph: {
      nodes: [
        { id: "research", label: "Research", order: 0 },
        { id: "plan", label: "Plan", order: 1 },
      ],
      edges: [{
        key: "research->plan",
        source: "research",
        target: "plan",
        kinds: ["normal"],
        contracts: ["quill.research.requirements/v1"],
      }],
    },
    contract_states: {
      research: { attempt: 2, state: "published", status: "COMPLETE", digest: "abc" },
    },
  };
  const graph = normalizePhaseGraph(run);
  assert.deepEqual(graph.edges[0].contracts, ["quill.research.requirements/v1"]);
  assert.deepEqual(graph.nodes[0].contractState, {
    attempt: 2,
    state: "published",
    kind: "",
    status: "COMPLETE",
    digest: "abc",
  });
  assert.equal(normalizeContractState({ attempt: 0, state: "published" }), null);
});
import { reconcileModelOverrides } from "../../quill_api/web/run-model-overrides.mjs";
import { linearTrend, sparklineLeftMargin } from "../../quill_api/web/trends.mjs";

test("run trend uses a least-squares line and never projects below zero", () => {
  assert.deepEqual(linearTrend([]), []);
  assert.deepEqual(linearTrend([7]), [7]);
  assert.deepEqual(linearTrend([10, 20, 30]), [10, 20, 30]);
  const descending = linearTrend([30, 10, 0]);
  assert.ok(Math.abs(descending[0] - 28.3333) < 0.001);
  assert.ok(Math.abs(descending[1] - 13.3333) < 0.001);
  assert.equal(descending[2], 0);
});

test("run trend reserves only the width required by its visible y-axis values", () => {
  assert.equal(sparklineLeftMargin(["0", "50", "100"]), 30);
  assert.equal(sparklineLeftMargin(["0s", "6h 17m 28s", "12h 34m 56s"]), 78);
  assert.equal(sparklineLeftMargin(["x".repeat(100)]), 96);
});

test("workflow refresh preserves valid run model overrides", () => {
  const phases = [
    { id: "research", model: "default" },
    { id: "plan", model: "default" },
  ];
  assert.deepEqual(
    reconcileModelOverrides(
      { enabled: true, overrides: { research: "alternate", plan: "alternate" } },
      phases,
      ["default", "alternate"],
      true,
    ),
    {
      enabled: true,
      overrides: { research: "alternate", plan: "alternate" },
    },
  );
});

test("workflow refresh resets overrides when the selected workflow changes", () => {
  assert.deepEqual(
    reconcileModelOverrides(
      { enabled: true, overrides: { plan: "alternate" } },
      [{ id: "plan", model: "default" }],
      ["default", "alternate"],
      false,
    ),
    { enabled: false, overrides: {} },
  );
});

const graphRun = {
  run_id: "r1",
  status: "running",
  phase: "review",
  phase_graph: {
    nodes: [
      { id: "impl", label: "Implement", type: "producer", order: 0 },
      { id: "review", label: "Review", type: "reviewer", order: 1 },
      { id: "ship", label: "Ship", type: "mechanical", order: 2 },
    ],
    edges: [
      { key: "impl->review", source: "impl", target: "review", kinds: ["normal", "retry"] },
      { key: "review->impl", source: "review", target: "impl", kinds: ["retry"] },
      { key: "review->ship", source: "review", target: "ship", kinds: ["normal"] },
    ],
  },
  phase_route_counts: { "impl->review": 2, "review->impl": 1, "review->ship": 0 },
  phase_durations: { impl: 12.2, review: 3.01 },
};

test("run elapsed time freezes at the terminal timestamp", () => {
  assert.equal(runElapsed(1_000, 1_125), "2m 05s");
  assert.equal(runElapsed(1_000, null, 1_125), "2m 05s");
  assert.equal(runElapsed(1_125, 1_000), "0s");
  assert.equal(runElapsed(null, 1_125), "—");
});

test("memory capacity uses two decimal GB precision", () => {
  assert.equal(formatMemoryGb(512), "0.50");
  assert.equal(formatMemoryGb(8448), "8.25");
  assert.equal(formatMemoryGb(32768), "32.00");
  assert.equal(formatMemoryGb(null), "—");
});

test("phase durations use compact minute and second labels", () => {
  assert.equal(formatPhaseDuration(0), "0m00s");
  assert.equal(formatPhaseDuration(31.01), "0m32s");
  assert.equal(formatPhaseDuration(125), "2m05s");
});

test("phase nodes share a width reserved for titles and maximum phase tokens", () => {
  const tokenReserved = phaseNodeWidth([{ id: "plan", displayId: "plan" }]);
  const titleExpanded = phaseNodeWidth([{ id: "long", displayId: "exceptionally_long_phase_title" }]);

  assert.equal(tokenReserved, 114);
  assert.ok(titleExpanded > tokenReserved);
});

test("phase graph metrics accumulate tokens but count only terminal executions", () => {
  const metrics = phaseGraphMetrics(
    [
      { phase: "plan", verdict: "BLOCK", total_tokens: 200, context_window_tokens: 100 },
      { phase: "plan", verdict: null, total_tokens: 80, context_window_tokens: 40 },
      { phase: "impl", verdict: null, total_tokens: 25 },
    ],
    { phase_usages: { plan: { total_tokens: 350, context_window_tokens: 175 } } },
  );

  assert.deepEqual(metrics.executionCounts, { plan: 1 });
  assert.deepEqual(metrics.tokenCounts, { plan: 175, impl: 25 });
});

test("phase graph normalizes counts and lays retry routes in orthogonal lanes", () => {
  const graph = normalizePhaseGraph(graphRun);
  assert.deepEqual(graph.edges.map((edge) => edge.count), [2, 1, 0]);
  assert.deepEqual(graph.nodes.map((node) => node.durationSeconds), [13, 4, null]);
  const layout = layoutPhaseGraph(graph);
  assert.equal(layout.edges[0].lane, 0);
  assert.equal(layout.edges[1].lane, 1);
  // Retry routes now meet a node below its centre line, so a normal edge and an opposing retry no
  // longer collide at the node edge and each keeps its own arrowhead.
  assert.ok(layout.edges.every((edge) => edge.showArrow), "every route draws its own arrow");
  assert.match(layout.edges[1].path, /^M .* H .* V .* H .* V .* H /);
  assert.doesNotMatch(layout.edges[1].path, /[CLQSA]/);
  const retrySource = layout.nodes.find((node) => node.id === layout.edges[1].source);
  const retryTarget = layout.nodes.find((node) => node.id === layout.edges[1].target);
  // Leaves and re-enters between the centre line and the base, not on the centre line.
  const attach = retryAttachY(retrySource);
  assert.ok(attach > retrySource.y + retrySource.height / 2);
  assert.ok(attach < retrySource.y + retrySource.height);
  assert.match(layout.edges[1].path, new RegExp(`^M ${retrySource.x + retrySource.width} ${attach} `));
  assert.match(layout.edges[1].path, new RegExp(`V ${retryAttachY(retryTarget)} H ${retryTarget.x}$`));
  // A retry runs backwards, so its legs step just outside each node rather than bending mid-gap:
  // out past the source's right edge, and in before the target's left edge, by the same inset.
  const [, exitX, enterX] = layout.edges[1].path.match(/H ([\d.]+) V .* H ([\d.]+) V/).map(Number);
  const exitInset = exitX - (retrySource.x + retrySource.width);
  const enterInset = retryTarget.x - enterX;
  const columnGap = layout.nodes[1].x - (layout.nodes[0].x + layout.nodes[0].width);
  assert.equal(exitInset, enterInset, "both legs use the same inset");
  assert.ok(exitInset > 0 && exitInset < columnGap, "legs stay inside the column gap");
  assert.match(orthogonalPath(layout.nodes[0], layout.nodes[1]), /^M .* H /);
});

test("phase graph hides unused retry loops and restores the primary flow arrow", () => {
  const run = {
    ...graphRun,
    phase_route_counts: { "impl->review": 1, "review->impl": 0, "review->ship": 0 },
  };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));

  assert.deepEqual(layout.edges.map((edge) => edge.key), ["impl->review", "review->ship"]);
  assert.equal(layout.edges[0].showArrow, true);
  assert.equal(layout.height, 122);
});

test("phase graph reserves vertical loop space without widening self-check phases", () => {
  const run = structuredClone(graphRun);
  run.phase_graph.nodes[0].self_check = true;
  const graph = normalizePhaseGraph(run);
  const layout = layoutPhaseGraph(graph);

  const extent = selfLoopLayout("SELF CHECK", 100, 64).extent;
  const plain = layoutPhaseGraph(normalizePhaseGraph(structuredClone(graphRun))).nodes[0].y;

  assert.equal(graph.nodes[0].selfCheck, true);
  assert.equal(layout.nodes[0].width, phaseNodeWidth(graph.nodes));
  // Asserted against the derived extent, not a literal, so tightening the loop cannot silently
  // invalidate the reservation this test exists to protect.
  assert.ok(layout.nodes[0].y >= extent, "self-check decoration must fit above the node");
  assert.ok(layout.nodes[0].y > plain, "self-check pushes the node below the plain baseline");
  assert.ok(layout.nodes[1].x > layout.nodes[0].x + layout.nodes[0].width);
});

test("phase graph hides an unused self-fix and reserves its full lower clearance after it runs", () => {
  const run = structuredClone(graphRun);
  run.phase_graph.nodes[0].self_check = true;
  run.phase_graph.nodes[0].self_fix = true;
  let graph = normalizePhaseGraph(run);
  let layout = layoutPhaseGraph(graph);

  const belowExtent = selfLoopLayout("SELF FIX", 100, 64, { below: true }).extent;
  assert.equal(graph.nodes[0].selfFixRan, false);
  assert.ok(layout.nodes[0].y >= belowExtent);

  run.self_fixes = { impl: "completed" };
  graph = normalizePhaseGraph(run);
  layout = layoutPhaseGraph(graph);

  assert.equal(graph.nodes[0].selfFixRan, true);
  assert.equal(graph.nodes[0].selfFixStatus, "completed");
  assert.ok(
    layout.height >= layout.nodes[0].y + layout.nodes[0].height + belowExtent,
    "canvas must reserve the full lower clearance once the self-fix has run",
  );
});

test("parallel phase lanes include self-loop offsets and badge heights in their spacing", () => {
  const run = structuredClone(graphRun);
  run.phase_graph.nodes = [
    { id: "audit.top", label: "Top", order: 0, column: 0, lane: 0, group: "audit", self_fix: true },
    { id: "audit.bottom", label: "Bottom", order: 1, column: 0, lane: 1, group: "audit", self_check: true },
    { id: "plain.top", label: "Plain top", order: 2, column: 1, lane: 0, group: "plain" },
    { id: "plain.bottom", label: "Plain bottom", order: 3, column: 1, lane: 1, group: "plain" },
    { id: "solo", label: "Solo", order: 4, column: 2, lane: 0 },
  ];
  run.phase_graph.edges = [];
  run.self_fixes = { "audit.top": "active" };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));
  const top = layout.nodes.find((node) => node.id === "audit.top");
  const bottom = layout.nodes.find((node) => node.id === "audit.bottom");
  const plainTop = layout.nodes.find((node) => node.id === "plain.top");
  const plainBottom = layout.nodes.find((node) => node.id === "plain.bottom");
  const solo = layout.nodes.find((node) => node.id === "solo");

  // Read the undecorated lane gap off the plain group, then assert the decorated group is exactly
  // that gap plus one decoration extent at each facing edge.
  const laneGap = plainBottom.y - (plainTop.y + plainTop.height);
  const extent = selfLoopLayout("SELF FIX", 100, 64).extent;
  assert.equal(bottom.y - (top.y + top.height), extent + laneGap + extent);
  assert.ok(laneGap > 0);
  assert.equal((top.y + bottom.y) / 2, solo.y);
  assert.equal((plainTop.y + plainBottom.y) / 2, solo.y);
});

test("phase rows show whether self-check is enabled and preserve blue active graph state", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const styles = await readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8");
  const format = await readFile(new URL("../../quill_api/web/format.mjs", import.meta.url), "utf8");

  assert.match(app, /configuredSelfCheck \? "enabled" : "disabled"/);
  assert.match(format, /"enabled"/);
  assert.match(format, /"disabled"/);
  assert.match(app, /const checkState = states\[node\.id\]/);
  assert.match(styles, /\.phase-node\[data-state="active"\] \{ color: var\(--cyan\); \}/);
  assert.match(styles, /\.phase-self-check-loop\[data-state="active"\] \{ color: var\(--cyan\); \}/);
});

test("phase graph renders execution and token labels inside nodes and labels only loops", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  assert.match(app, /executionCount\.textContent = `x\$\{node\.executionCount\}`/);
  assert.match(app, /class: "phase-node-tokens"/);
  assert.match(app, /class: "phase-self-check-loop"/);
  assert.match(app, /class: "phase-self-fix-loop"/);
  assert.match(app, /if \(node\.selfFixRan\)/);
  // The loop geometry lives in phase-graph.mjs so the reserved space can be derived from it.
  // Assert app.mjs delegates rather than re-deriving a path it could drift from.
  assert.match(app, /selfLoopLayout\("SELF CHECK", mainWidth, node\.height\)/);
  assert.match(app, /selfLoopLayout\("SELF FIX", mainWidth, node\.height, \{ below: true \}\)/);
  assert.doesNotMatch(app, /V \$\{node\.height \+ 38\}/, "no hardcoded loop reach in app.mjs");
  // Duration now sits inside the node on the shared top row, not floating above it.
  assert.match(app, /const rows = nodeTextRows\(node\.height\)/);
  assert.doesNotMatch(app, /y: -9,/, "duration must not be positioned above the node");
  assert.doesNotMatch(app, /phase-self-check-node/);
  assert.doesNotMatch(app, /phase-edge-contract/);
  assert.doesNotMatch(app, /contracts:/);
  assert.match(app, /edge\.lane > 0 && edge\.count > 0/);
});

test("phase graph state precedence covers active, complete, unvisited, and failed", () => {
  const graph = normalizePhaseGraph(graphRun);
  assert.deepEqual(phaseNodeStates(graphRun, graph), {
    impl: "completed",
    review: "active",
    ship: "unvisited",
  });
  assert.equal(phaseNodeStates({ ...graphRun, status: "failed" }, graph).review, "failed");
  assert.deepEqual(phaseNodeStates({
    ...graphRun,
    phase: "impl",
    active_phases: { impl: 10 },
    history: [
      { phase: "impl", verdict: "DONE" },
      { phase: "review", verdict: "BLOCK" },
    ],
  }, graph), {
    impl: "active",
    review: "failed",
    ship: "unvisited",
  });
  assert.deepEqual(phaseNodeStates({ ...graphRun, status: "done" }, graph), {
    impl: "completed",
    review: "completed",
    ship: "completed",
  });
});

test("retry edges follow the repair target while normal edges follow their source", () => {
  const states = { gate: "active", repair: "completed", next: "unvisited" };
  assert.equal(phaseEdgeState({
    source: "gate", target: "repair", kinds: ["retry"],
  }, states), "completed");
  assert.equal(phaseEdgeState({
    source: "gate", target: "repair", kinds: ["retry"],
  }, { ...states, repair: "active" }), "active");
  assert.equal(phaseEdgeState({
    source: "gate", target: "repair", kinds: ["retry"],
  }, { ...states, repair: "failed" }), "failed");
  assert.equal(phaseEdgeState({
    source: "repair", target: "next", kinds: ["normal"],
  }, states), "completed");
});

test("phase graph handles missing and single-node topology without invented routes", () => {
  assert.equal(normalizePhaseGraph({}), null);
  const graph = normalizePhaseGraph({
    phase_graph: { nodes: [{ id: "plan", label: "Plan", type: "producer", order: 0 }], edges: [] },
    phase_route_counts: {},
  });
  assert.equal(layoutPhaseGraph(graph).edges.length, 0);
  assert.deepEqual(layoutPhaseGraph({ nodes: [], edges: [] }), {
    nodes: [], edges: [], groups: [], width: 360, height: 56,
  });
});

test("active phase centering clamps to both horizontal scroll boundaries", () => {
  assert.equal(centeredPhaseScroll(50, 300, 1000), 0);
  assert.equal(centeredPhaseScroll(500, 300, 1000), 350);
  assert.equal(centeredPhaseScroll(950, 300, 1000), 700);
  assert.equal(centeredPhaseScroll(150, 500, 300), 0);
});

test("phase graph structure signatures ignore live values but detect geometry changes", () => {
  const baseRun = {
    ...graphRun,
    phase_route_counts: { ...graphRun.phase_route_counts, "review->impl": 0 },
  };
  const base = normalizePhaseGraph(baseRun);
  const liveUpdate = normalizePhaseGraph({
    ...baseRun,
    phase_route_counts: { ...baseRun.phase_route_counts, "impl->review": 9 },
    phase_durations: { impl: 99, review: 40 },
    phase_token_counts: { impl: 50_000 },
  });
  assert.equal(phaseGraphStructureSignature(base), phaseGraphStructureSignature(liveUpdate));

  const contractMetadataUpdate = structuredClone(baseRun);
  contractMetadataUpdate.phase_graph.edges[0].contracts = ["quill.implementation/v2"];
  assert.equal(
    phaseGraphStructureSignature(base),
    phaseGraphStructureSignature(normalizePhaseGraph(contractMetadataUpdate)),
  );

  const withVisibleRetry = normalizePhaseGraph({
    ...baseRun,
    phase_route_counts: { ...baseRun.phase_route_counts, "review->impl": 1 },
  });
  assert.notEqual(phaseGraphStructureSignature(base), phaseGraphStructureSignature(withVisibleRetry));

  const selfFixRun = structuredClone(baseRun);
  selfFixRun.phase_graph.nodes[0].self_fix = true;
  selfFixRun.self_fixes = { impl: "active" };
  assert.notEqual(
    phaseGraphStructureSignature(base),
    phaseGraphStructureSignature(normalizePhaseGraph(selfFixRun)),
  );
});

test("discrete live updates reconcile mounted regions without rebuilding main", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const events = app.slice(app.indexOf("function connectEvents"), app.indexOf("async function connectTelemetry"));
  const updater = app.slice(app.indexOf("function updateLiveRegions"), app.indexOf("async function connectTelemetry"));
  const inspector = app.slice(app.indexOf("async function refreshRunInspector"), app.indexOf("function updateLiveRegions"));

  assert.match(events, /updateLiveRegions\(event\.run_id\)/);
  assert.match(events, /if \(isProgress\) \{\s*updateProgressRegions\(event\.run_id\);\s*return;/);
  assert.doesNotMatch(updater, /\brender\(\)/);
  assert.doesNotMatch(inspector, /\brender\(\)/);
  assert.match(inspector, /state\.route\.section !== "runs" \|\| state\.route\.id !== runId/);
  assert.match(app, /data-live-region|dataset\.liveRegion/);
  assert.match(app, /currentWrap\.scrollLeft = Math\.min\(priorScroll/);
  assert.doesNotMatch(app, /currentWrap\.scrollLeft\s*=\s*0/);
  assert.match(app, /"Phase", "Total Run Time", "Updated"/);
});

test("phase graph staggers concurrent audit lanes and marks each active lane", () => {
  const run = {
    status: "running",
    active_phases: { "review.architecture": 1, "review.correctness": 2, "review.tests": 3 },
    phase_graph: {
      nodes: [
        { id: "impl", label: "Implement", order: 0, column: 0, lane: 0 },
        { id: "review.architecture", label: "Architecture", order: 1, column: 1, lane: 0, group: "review" },
        { id: "review.correctness", label: "Correctness", order: 2, column: 1, lane: 1, group: "review" },
        { id: "review.tests", label: "Tests", order: 3, column: 1, lane: 2, group: "review" },
        { id: "gate", label: "Gate", order: 4, column: 2, lane: 0 },
      ],
      edges: [
        { source: "impl", target: "review.architecture", kinds: ["normal"] },
        { source: "impl", target: "review.correctness", kinds: ["normal"] },
        { source: "impl", target: "review.tests", kinds: ["normal"] },
        { source: "review.architecture", target: "gate", kinds: ["normal"] },
        { source: "review.correctness", target: "gate", kinds: ["normal"] },
        { source: "review.tests", target: "gate", kinds: ["normal"] },
      ],
    },
  };
  const graph = normalizePhaseGraph(run);
  const layout = layoutPhaseGraph(graph);
  const lanes = layout.nodes.filter((node) => node.group === "review");

  assert.equal(new Set(lanes.map((node) => node.x)).size, 3);
  assert.equal(lanes[1].x - lanes[0].x, lanes[0].width / 4);
  assert.equal(lanes[2].x - lanes[1].x, lanes[1].width / 4);
  // Grouped lanes start below PRIMARY_Y so the container caption has a band to sit in; the
  // ungrouped neighbours re-centre against them.
  assert.deepEqual(lanes.map((node) => node.y), [60, 158, 256]);
  assert.equal(layout.nodes.find((node) => node.id === "impl").y, 158);
  assert.equal(layout.nodes.find((node) => node.id === "gate").y, 158);
  const [container] = layout.groups;
  assert.equal(container.label, "review");
  assert.ok(container.y < 60 && container.y + container.height > 256 + 64);
  assert.deepEqual(lanes.map((node) => node.displayId), ["architecture", "correctness", "tests"]);
  assert.deepEqual(phaseNodeStates(run, graph), {
    impl: "unvisited",
    "review.architecture": "active",
    "review.correctness": "active",
    "review.tests": "active",
    gate: "unvisited",
  });
});

test("phase graph completes an independently finished parallel lane", () => {
  const run = {
    status: "running",
    // `phase` remains the last phase that started, but the authoritative active map has already
    // removed tests after its terminal event arrived.
    phase: "review.tests",
    active_phases: { "review.correctness": 2 },
    history: [{ phase: "review.tests", verdict: "PASS" }],
    phase_graph: {
      nodes: [
        { id: "review.correctness", label: "Correctness", order: 0, column: 0, lane: 0, group: "review" },
        { id: "review.tests", label: "Tests", order: 1, column: 0, lane: 1, group: "review" },
      ],
      edges: [],
    },
  };
  const graph = normalizePhaseGraph(run);

  assert.deepEqual(phaseNodeStates(run, graph), {
    "review.correctness": "active",
    "review.tests": "completed",
  });
});

test("phase graph retains the current-phase fallback for legacy runs without an active map", () => {
  const run = {
    status: "running",
    phase: "plan",
    phase_graph: {
      nodes: [{ id: "plan", label: "Plan", order: 0, column: 0, lane: 0 }],
      edges: [],
    },
  };
  const graph = normalizePhaseGraph(run);

  assert.deepEqual(phaseNodeStates(run, graph), { plan: "active" });
});

test("phase graph inserts only observed model loads and keeps the waiting phase inactive", () => {
  const run = {
    status: "running",
    phase: "review",
    phase_started_at: null,
    active_phases: {},
    model_loads: [
      {
        load_id: "model-load-1",
        phase: "plan",
        model: "qwen-35b",
        status: "completed",
        started_at: 10,
        duration_s: 32.4,
      },
      {
        load_id: "model-load-2",
        phase: "review",
        model: "gemma-31b",
        status: "active",
        started_at: 50,
        duration_s: null,
      },
    ],
    phase_graph: {
      nodes: [
        { id: "plan", label: "Plan", order: 0, column: 0, lane: 0 },
        { id: "review", label: "Review", order: 1, column: 1, lane: 0 },
      ],
      edges: [{ key: "plan->review", source: "plan", target: "review", kinds: ["normal"] }],
    },
  };

  const graph = normalizePhaseGraph(run);
  const layout = layoutPhaseGraph(graph);
  const loads = graph.nodes.filter((node) => node.type === "model_load");
  const states = phaseNodeStates(run, graph);

  assert.deepEqual(loads.map((node) => [node.modelName, node.column, node.executionCount]), [
    ["qwen-35b", 0, 1],
    ["gemma-31b", 2, 0],
  ]);
  assert.equal(graph.nodes.find((node) => node.id === "plan").column, 1);
  assert.equal(graph.nodes.find((node) => node.id === "review").column, 3);
  const entryLoad = layout.nodes.find((node) => node.id === "__model_load__1");
  const plan = layout.nodes.find((node) => node.id === "plan");
  const laterLoad = layout.nodes.find((node) => node.id === "__model_load__2");
  const review = layout.nodes.find((node) => node.id === "review");
  assert.equal(plan.x - (entryLoad.x + entryLoad.width), entryLoad.width + 27);
  assert.equal(review.x - (laterLoad.x + laterLoad.width), 27);
  assert.deepEqual(graph.edges.map((edge) => [edge.source, edge.target]).sort(), [
    ["__model_load__2", "review"],
    ["plan", "__model_load__2"],
  ].sort());
  assert.equal(states.__model_load__1, "completed");
  assert.equal(states.__model_load__2, "active");
  assert.equal(states.review, "unvisited");
});

test("phase graph attaches a parent audit model load to every concurrent lane", () => {
  const graph = normalizePhaseGraph({
    model_loads: [{ phase: "review_impl", model: "qwen", status: "completed", duration_s: 20 }],
    phase_graph: {
      nodes: [
        { id: "impl", label: "Implement", order: 0, column: 0, lane: 0 },
        { id: "review_impl.architecture", label: "Architecture", order: 1, column: 1, lane: 0, group: "review_impl" },
        { id: "review_impl.tests", label: "Tests", order: 2, column: 1, lane: 1, group: "review_impl" },
      ],
      edges: [
        { source: "impl", target: "review_impl.architecture", kinds: ["normal"] },
        { source: "impl", target: "review_impl.tests", kinds: ["normal"] },
      ],
    },
  });
  const load = graph.nodes.find((node) => node.type === "model_load");

  assert.deepEqual(load.targetIds, ["review_impl.architecture", "review_impl.tests"]);
  assert.deepEqual(
    graph.edges.filter((edge) => edge.source === load.id).map((edge) => edge.target).sort(),
    ["review_impl.architecture", "review_impl.tests"],
  );
});

test("phase graph recovers parallel audit layout when an older API omits layout metadata", () => {
  const graph = normalizePhaseGraph({
    phase_graph: {
      nodes: [
        { id: "impl", label: "Implement", order: 0, column: null, lane: 0, group: null },
        { id: "review.architecture", label: "Requirements + architecture", order: 1, column: null, lane: 0, group: null },
        { id: "review.correctness", label: "Correctness + lifecycle", order: 2, column: null, lane: 0, group: null },
        { id: "review.tests", label: "Tests + regressions", order: 3, column: null, lane: 0, group: null },
        { id: "gate", label: "Implementation gate", order: 4, column: null, lane: 0, group: null },
      ],
      edges: [],
    },
  });
  const layout = layoutPhaseGraph(graph);
  const audits = layout.nodes.filter((node) => node.group === "review");

  assert.deepEqual(audits.map((node) => [node.column, node.lane, node.displayId]), [
    [1, 0, "architecture"],
    [1, 1, "correctness"],
    [1, 2, "tests"],
  ]);
  assert.equal(layout.nodes.find((node) => node.id === "gate").column, 2);
});

test("telemetry formatters handle unavailable values and heat boundaries", () => {
  assert.equal(formatPercent(null), "N/A");
  assert.equal(formatPercent(undefined), "N/A");
  assert.equal(formatPercent(101), "100%");
  assert.equal(formatTemperature(42.25), "42.3°C");
  assert.equal(formatTemperature(undefined), "N/A");
  assert.equal(temperatureTone(44), "cool");
  assert.equal(temperatureTone(45), "warm");
  assert.equal(temperatureTone(85), "hot");
});

test("telemetry UI uses persisted scales and horizontal in-bar readings", async () => {
  const [app, api, styles, index] = await Promise.all([
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/api.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/index.html", import.meta.url), "utf8"),
  ]);
  assert.match(index, /data-route="settings"/);
  assert.match(api, /telemetrySettings:.*\/settings\/telemetry/s);
  assert.match(api, /updateTelemetrySettings:.*method: "PUT"/s);
  assert.match(app, /cpu_temperature_min_c: 20/);
  assert.match(app, /cpu_temperature_max_c: 70/);
  assert.match(app, /gpu_temperature_max_c: 80/);
  assert.match(app, /Threadripper\\b/);
  assert.match(app, /gaugeMetric\("temperature", "TEMP"\)/);
  assert.match(app, /gaugeMetric\("fan", "FAN"\)/);
  assert.match(app, /--temperature-load/);
  assert.match(styles, /\.gauge-horizontal-well/);
  assert.match(styles, /\.gauge-value[^}]*position: absolute/);
  assert.match(styles, /\.gauge-load \{ --bar-color: var\(--cyan\)/);
  assert.match(styles, /\.gauge-memory \{ --bar-color: var\(--memory-color, var\(--green\)\)/);
  assert.match(app, /const memoryHue = memoryPercent === null \? 120 : 120 \* \(1 - memoryPercent \/ 100\)/);
  assert.match(styles, /\.gauge-memory \{[^}]*linear-gradient\(to right, var\(--green\) 0%, var\(--amber\) 55%, var\(--red\) 100%\)/);
  assert.match(styles, /\.gauge-temperature \{ --bar-color: var\(--temperature-color, var\(--green\)\)/);
  assert.match(styles, /\.gauge-fan \{ --bar-color: var\(--cyan\); --bar-end: var\(--violet\)/);
  assert.match(app, /--fan-load/);
  assert.doesNotMatch(app, /fanHue|--fan-color/);
  assert.match(styles, /\.gauge-label \{[^}]*height: 2\.5em;[^}]*overflow: hidden/);
  assert.match(styles, /\.gauge-label > span \{[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap/);
  assert.match(app, /const temperatureHue = 120 \* \(1 - temperatureLoad \/ 100\)/);
  assert.match(styles, /clip-path: inset\(0 calc\(100% - var\(--bar-load\) \* 1%\) 0 0\)/);
});

test("system telemetry header shows the loaded model and rolling vLLM rates", async () => {
  const source = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const styles = await readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8");
  assert.match(source, /renderVllmThroughput/);
  assert.match(source, /processing_tokens_per_second/);
  assert.match(source, /generation_tokens_per_second/);
  assert.match(source, /loaded_models/);
  assert.match(source, /throughputMetric\("model", "MODEL"\)/);
  assert.match(source, /toFixed\(1\).*tok\/s/s);
  assert.match(styles, /\.vllm-throughput/);
});

test("run failures present the stable category before raw diagnostic detail", async () => {
  const source = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  assert.match(source, /run\.failure_label \? `\$\{run\.failure_label\}\\n\$\{run\.error\}`/);
});

test("telemetry cards identify supported hardware vendors and use title-case panels", async () => {
  const [source, styles] = await Promise.all([
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8"),
  ]);

  assert.match(source, /function hardwareVendor/);
  assert.match(source, /return "AMD"/);
  assert.match(source, /return "NVIDIA"/);
  assert.match(source, /return "INTEL"/);
  assert.match(source, /const badges = \{/);
  assert.match(source, /element\("img", "hardware-vendor"\)/);
  assert.match(source, /panel\("System Telemetry"/);
  assert.match(source, /panel\("Recent Signals"/);
  assert.doesNotMatch(source, /panel\("System telemetry"|panel\("Recent signals"/);
  assert.match(styles, /\.hardware-vendor\[data-vendor="amd"\]/);
  assert.match(styles, /\.hardware-vendor \{[^}]*object-fit: contain/);
  assert.doesNotMatch(styles, /\.hardware-vendor \{[^}]*(?:border|background|box-shadow):/);
});

test("frontend uses dedicated pushed telemetry and no routine polling", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  assert.match(app, /EventSource\("\/telemetry\/events"\)/);
  assert.match(app, /addEventListener\("telemetry"/);
  assert.doesNotMatch(app, /className[^\n]*equalizer|element\("div", "equalizer"\)/);
  assert.doesNotMatch(app, /setInterval\([^\n]*refreshRuns/);
  assert.doesNotMatch(app, /setInterval\([^\n]*refreshSystem/);
});

test("buildQuery omits blank filters and encodes values", () => {
  assert.equal(
    buildQuery({ repo: "me/project name", ticket: "", status: null, limit: 200 }),
    "?repo=me%2Fproject+name&limit=200",
  );
});

test("path helpers encode resource identifiers without losing nested paths", () => {
  assert.equal(segment("ticket/one"), "ticket%2Fone");
  assert.equal(relativePath("wiki/getting started.md"), "wiki/getting%20started.md");
  assert.equal(relativePath("/wiki//nested/file.md"), "wiki/nested/file.md");
});

test("FastAPI errors normalize strings and validation arrays", () => {
  assert.equal(errorMessage({ detail: "not found" }), "not found");
  assert.equal(
    errorMessage({ detail: [{ loc: ["body", "reason"], msg: "too short" }] }),
    "reason: too short",
  );
  assert.equal(errorMessage({ error: "internal_error" }), "internal_error");
});

test("apiFetch serializes JSON and returns structured errors", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ detail: "conflict" }), {
      status: 409,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await assert.rejects(
      apiFetch("/thing", { method: "DELETE", body: { reason: "obsolete" } }),
      (error) => error instanceof ApiError && error.status === 409 && error.message === "conflict",
    );
    assert.equal(request.path, "/thing");
    assert.equal(request.options.body, JSON.stringify({ reason: "obsolete" }));
    assert.equal(request.options.headers.get("content-type"), "application/json");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("hash routes preserve resource identifiers", () => {
  assert.deepEqual(parseRoute("#/runs/run%2Fone"), { section: "runs", id: "run/one" });
  assert.deepEqual(parseRoute("#/skills/box3d"), { section: "skills", id: "box3d" });
  assert.deepEqual(parseRoute("#/workspaces"), { section: "workspaces", id: null });
  assert.deepEqual(parseRoute("#/queue"), { section: "queue", id: null });
  assert.deepEqual(parseRoute("#/memories"), { section: "memories", id: null });
  assert.deepEqual(parseRoute("#/unknown"), { section: "overview", id: null });
});

test("project queue helpers keep only queue-capable repositories and eligible selections", () => {
  assert.deepEqual(
    queueCapableRepositories([
      { name: "me/ready", project_board: "Ready" },
      { name: "me/blank", project_board: "  " },
      { name: "me/unconfigured", project_board: null },
    ]).map((repository) => repository.name),
    ["me/ready"],
  );
  const groups = [{
    epic_number: 1,
    epic_title: "Epic",
    tickets: [
      { number: 3, selectable: true },
      { number: 4, selectable: false },
      { number: 16, selectable: true },
    ],
  }];
  assert.deepEqual([...pruneQueueSelection(groups, new Set(["3", "4", "99"]))], ["3"]);
  assert.deepEqual(groupSelectionState(groups[0], new Set()), {
    checked: false, indeterminate: false, disabled: false,
  });
  assert.deepEqual(groupSelectionState(groups[0], new Set(["3"])), {
    checked: false, indeterminate: true, disabled: false,
  });
  assert.deepEqual(groupSelectionState(groups[0], new Set(["3", "16"])), {
    checked: true, indeterminate: false, disabled: false,
  });
  assert.deepEqual(groupSelectionState({ tickets: [{ number: 4, selectable: false }] }, new Set()), {
    checked: false, indeterminate: false, disabled: true,
  });
});

test("chooseSelection keeps a still-present value and falls back to the first option", () => {
  assert.equal(chooseSelection(["main", "feature"], "feature"), "feature");
  assert.equal(chooseSelection(["main", "feature"], "gone"), "main");
  assert.equal(chooseSelection([], "anything"), "");
  // Comparison is by identifier value, never by list position.
  assert.equal(chooseSelection([126, 127], "127"), "127");
});

test("run actions and tones follow authoritative status", () => {
  for (const status of ["queued", "running", "needs_decision"]) assert.equal(canStopRun(status), true);
  for (const status of ["done", "failed", "halted"]) assert.equal(canStopRun(status), false);
  assert.equal(canAnswerRun("needs_decision"), true);
  assert.equal(canAnswerRun("running"), false);
  assert.equal(statusTone("running"), "active");
  assert.equal(statusTone("done"), "success");
  assert.equal(statusTone("failed"), "danger");
});

test("live run label prefers truthful internal activity over configured phase", () => {
  const run = {
    status: "running",
    phase: "branch",
    phase_label: "create branch",
    activity: "loading_model",
    activity_label: "Loading model Qwen3.6_27B_FP8",
  };
  assert.equal(liveRunLabel(run), "Loading model Qwen3.6_27B_FP8");
  assert.equal(liveRunLabel({ ...run, activity_label: null }), "create branch");
});

test("branch names follow the selected work type and issue title", () => {
  assert.equal(
    branchName("enhancement", { number: 127, title: "Implement Vllm Capabilities" }),
    "enhancement/implement-vllm-capabilities_127",
  );
  assert.equal(
    branchName("bug", { number: 126, title: "Fix Source Of Model Truth" }),
    "bug/fix-source-of-model-truth_126",
  );
  assert.equal(
    branchName("Priority: High", { number: 126, title: "Fix Source Of Model Truth" }),
    "priority-high/fix-source-of-model-truth_126",
  );
});

test("specific bug labels beat broad enhancement and documentation labels", () => {
  const types = ["enhancement", "feat", "bug", "documentation"];
  assert.equal(
    preferredWorkType({ labels: ["documentation", "enhancement"] }, types),
    "enhancement",
  );
  assert.equal(
    preferredWorkType({ labels: ["bug", "documentation", "enhancement"] }, types),
    "bug",
  );
});

test("catalog validation mirrors server boundaries", () => {
  assert.equal(validCatalogName("review-plan"), true);
  assert.equal(validCatalogName("../escape"), false);
  assert.equal(validCatalogName(""), false);
  assert.equal(validReason("why"), true);
  assert.equal(validReason("no"), false);
  assert.equal(validReason("x".repeat(201)), false);
});

test("formatters cover small and boundary values", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(1024), "1.0 KB");
  assert.equal(formatDuration(65), "1m 05s");
  assert.equal(formatDuration(-4), "0s");
});

test("diagnostic summaries prefer the first actionable error and truncate noise", () => {
  const output = "$ ./build.sh --test\n[ 58%] compiling\nfatal error: missing/header.h: No such file";
  assert.equal(diagnosticSummary(output), "fatal error: missing/header.h: No such file");
  assert.equal(diagnosticSummary("first line\nmore output"), "first line");
  assert.equal(diagnosticSummary("x".repeat(200), 20), `${"x".repeat(19)}…`);
});

test("external links allow only HTTP protocols", () => {
  assert.equal(safeExternalUrl("https://github.com/example/repo/pull/1"), "https://github.com/example/repo/pull/1");
  assert.equal(safeExternalUrl("javascript:alert(1)"), null);
  assert.equal(safeExternalUrl("not a url"), null);
});

test("frontend starts runs without uploading config text", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ run_id: "run-1", status: "queued" }), {
      status: 202,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await QuillApi.start({ repo: "me/proj", branch: "ticket-1", ticket: 1, mode: "create" });
    assert.equal(request.path, "/runs");
    assert.equal(request.options.method, "POST");
    assert.deepEqual(JSON.parse(request.options.body), {
      repo: "me/proj",
      branch: "ticket-1",
      ticket: 1,
      mode: "create",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  const [api, app, index] = await Promise.all([
    readFile(new URL("../../quill_api/web/api.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/index.html", import.meta.url), "utf8"),
  ]);
  const source = `${api}\n${app}\n${index}`;
  assert.match(source, /apiFetch\(["'`]\/runs["'`],\s*\{\s*method:\s*["'`]POST/);
  assert.match(source, /Start run/i);
  assert.doesNotMatch(source, /Clear prefix cache|Clear cache between phases|Cold cache/);
  assert.doesNotMatch(app, /clear_prefix_cache/);
  assert.doesNotMatch(source, /config:\s*state\.runDraft/);
  assert.match(source, /githubRepositories/);
  assert.match(source, /Generated branch/);
  assert.doesNotMatch(source, /Quill loads quillfolio\.toml from the root/);
  assert.match(source, /start-run-actions/);
  assert.match(source, /delete state\.errors\["github-update-target"\]/);
  assert.match(source, /const branchRefresh = refreshDraftBranch\(\);\s*render\(\);\s*await branchRefresh/);
  assert.match(source, /excluded_issue_labels/);
  assert.match(source, /applyIssueLabelFilter/);
  assert.match(source, /workType\.input\.disabled = state\.github\.work_types\.length <= 1/);
  assert.match(source, /state\.runDraft\.work_type = workType\.input\.value;\s*const branchRefresh = refreshDraftBranch\(\);\s*render\(\);\s*await branchRefresh/);
  assert.match(source, /&& !openSelect\) render\(\)/);
  assert.match(source, /event\.target instanceof HTMLSelectElement/);
});

test("pull request review reuses PR targeting without requiring new feedback", async () => {
  const [source, apiSource] = await Promise.all([
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/api.mjs", import.meta.url), "utf8"),
  ]);
  assert.match(source, /new Set\(\["update", "review"\]\)/);
  assert.match(source, /state\.runDraft\.mode === "update"/);
  assert.match(source, /review head/);
  assert.match(apiSource, /require_feedback/);
});

test("run form supports global and per-phase model overrides", async () => {
  const [source, styles] = await Promise.all([
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8"),
  ]);
  const start = source.slice(source.indexOf("function renderStartRun"), source.indexOf("function renderRunFilters"));
  assert.match(start, /Override models for this run/);
  assert.match(start, /"All phases"/);
  assert.match(start, /selectedWorkflow\?\.phases/);
  assert.match(start, /Object\.fromEntries\(modelPhases\.map/);
  assert.match(start, /candidate\.parallel_group === phase\.parallel_group/);
  assert.match(start, /linked\.map\(\(candidate\) => \[candidate\.id, phaseModel\.input\.value\]\)/);
  assert.match(start, /model_overrides: state\.runDraft\.override_models/);
  assert.match(styles, /\.model-override-grid/);
});

test("workspace client methods build exact nested URLs and methods", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options?.method || "GET" });
    return new Response(JSON.stringify({ workspaces: [], branches: [], message: "ok", branch: "main", repo: "me/proj" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await QuillApi.workspaces();
    await QuillApi.workspaceBranches("me/proj");
    await QuillApi.pullWorkspaceBranch("me/proj", "feature/x");
    await QuillApi.deleteWorkspaceBranch("me/proj", "feature/x");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calls, [
    { path: "/workspaces", method: "GET" },
    { path: "/workspaces/me/proj/branches", method: "GET" },
    // Slash-bearing branch names keep their separators (each segment encoded), not %2F.
    { path: "/workspaces/me/proj/branches/feature/x/pull", method: "POST" },
    { path: "/workspaces/me/proj/branches/feature/x", method: "DELETE" },
  ]);
});

test("project queue client methods preserve repository paths and batch bodies", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options?.method || "GET", body: options?.body || null });
    return new Response(JSON.stringify({ batches: [], groups: [], results: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await QuillApi.projectQueue();
    await QuillApi.projectQueueCandidates("me/proj");
    await QuillApi.addProjectQueueBatch("me/proj", [3, 16]);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calls, [
    { path: "/project-queue", method: "GET", body: null },
    { path: "/project-queue/me/proj/candidates", method: "GET", body: null },
    { path: "/project-queue/me/proj", method: "POST", body: JSON.stringify({ tickets: [3, 16] }) },
  ]);
});

test("runs client bulk deletes selected ids", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return new Response(JSON.stringify({ deleted: ["run-1", "run-2"] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await QuillApi.deleteRuns(["run-1", "run-2"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(request.path, "/runs");
  assert.equal(request.options.method, "DELETE");
  assert.deepEqual(JSON.parse(request.options.body), { run_ids: ["run-1", "run-2"] });
});

test("memories client lists and bulk deletes selected or all history", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options?.method || "GET", body: options?.body });
    return new Response(JSON.stringify(path === "/memories" && !options?.method
      ? { memories: [], archived_events: 0 }
      : { deleted: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await QuillApi.memories();
    await QuillApi.deleteMemories(["memory-1"], false);
    await QuillApi.deleteMemories([], true);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calls.map(({ path, method }) => ({ path, method })), [
    { path: "/memories", method: "GET" },
    { path: "/memories", method: "DELETE" },
    { path: "/memories", method: "DELETE" },
  ]);
  assert.deepEqual(JSON.parse(calls[1].body), { memory_ids: ["memory-1"], delete_all: false });
  assert.deepEqual(JSON.parse(calls[2].body), { memory_ids: [], delete_all: true });
});

test("memories page exposes repository filtering and selected or complete deletion", async () => {
  const [app, index] = await Promise.all([
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/index.html", import.meta.url), "utf8"),
  ]);
  assert.match(index, /data-route="memories"/);
  assert.match(app, /state\.route\.section === "memories"\) main\.append\(renderMemories\(\)\)/);
  assert.match(app, /Select all visible memories/);
  assert.match(app, /Delete selected/);
  assert.match(app, /Delete all/);
  assert.match(app, /state\.memoryRepo/);
  assert.match(app, /memory\.changed_files/);
});

test("runs page exposes dropdown filters and bulk selection controls", async () => {
  const [source, styles] = await Promise.all([
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8"),
  ]);
  assert.match(source, /Any repository/);
  assert.match(source, /Any ticket/);
  assert.match(source, /Select all visible runs/);
  assert.match(source, /Delete selected/);
  assert.match(source, /runFilters: \{ repo: "", ticket: "", status: "", offset: 0 \}/);
  assert.match(source, /limit: 200/);
  assert.match(source, /renderRunPagination/);
  assert.doesNotMatch(source, /Apply filters|Run inspector|selectField\("Limit"/);
  assert.match(source, /run\.phase_label \|\| run\.phase \|\| "—"/);
  assert.match(styles, /\[hidden\] \{ display: none !important; \}/);
});

test("run controls and filters render only on the history index, not selected-run detail", async () => {
  const source = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const detailBranch = source.indexOf("if (state.route.id)");
  const startRunPanel = source.indexOf("append(fragment, renderStartRun())");
  const historyBranch = source.indexOf("append(fragment, renderRunFilters())");

  assert.ok(detailBranch >= 0);
  assert.ok(startRunPanel > detailBranch);
  assert.ok(startRunPanel < historyBranch);
  assert.ok(historyBranch > detailBranch);
  assert.ok(source.slice(detailBranch, startRunPanel).includes("} else {"));
});

test("historical run status restores phase history from the persisted breakdown", async () => {
  const source = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const status = source.slice(source.indexOf("function renderRunStatus"), source.indexOf("function detailGrid"));

  assert.match(status, /state\.breakdown\?\.phase_executions/);
  assert.match(status, /persistedHistory\.length \? persistedHistory : \(run\.history \|\| \[\]\)/);
  assert.match(status, /run-status-history/);
});

test("eligible terminal runs expose restart controls on exact phase rows", async () => {
  const [api, app] = await Promise.all([
    readFile(new URL("../../quill_api/web/api.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
  ]);
  assert.match(api, /restartOptions:.*restart-options/);
  assert.match(api, /restart:.*\/restart/);
  assert.match(app, /state\.restartOptions\?\.eligible/);
  assert.match(app, /restartPhaseCell/);
  assert.match(app, /QuillApi\.restart\(runId, choice\.id, choice\.sequence\)/);
  assert.match(app, /Restart from.*execution/);
  assert.match(app, /run\.source_run_id/);
});

test("artifacts render as downloadable rows with one archive action and no viewer", async () => {
  const source = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const artifacts = source.slice(source.indexOf("function renderArtifacts"), source.indexOf("function branchMarker"));

  assert.match(artifacts, /artifactsArchiveUrl/);
  assert.match(artifacts, /artifactDownloadUrl/);
  assert.match(artifacts, /Download all/);
  assert.match(artifacts, /table-wrap/);
  assert.doesNotMatch(artifacts, /Copy|artifact-viewer|select/);
});

test("workspaces page wires both dropdowns, guarded refresh, and simple delete confirmation", async () => {
  const [api, app, index] = await Promise.all([
    readFile(new URL("../../quill_api/web/api.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/index.html", import.meta.url), "utf8"),
  ]);
  const source = `${api}\n${app}\n${index}`;
  // Nav entry and route dispatch.
  assert.match(index, /data-route="workspaces"/);
  assert.match(app, /state\.route\.section === "workspaces"\) main\.append\(renderWorkspaces\(\)\)/);
  // Two dependent dropdowns: a workspace select and a branch select.
  assert.match(app, /choiceField\(\s*["'`]Workspace/);
  assert.match(app, /choiceField\(\s*["'`]Branch/);
  // Both actions are present.
  assert.match(app, /Fetch & pull/);
  assert.match(app, /Delete local branch/);
  // Deletion keeps a normal confirm/cancel modal without requiring typed text.
  assert.match(app, /confirmAction\(`Delete local \$\{name\}`, message\)/);
  assert.doesNotMatch(app, /confirmAction\(`Delete local \$\{name\}`, message, name\)/);
  // Refresh honours the shared open-select guard so background renders can't close a dropdown.
  assert.match(app, /state\.route\.section === "workspaces" && !openSelect\) render\(\)/);
  // No polling timer is wired for the workspace refreshers.
  assert.doesNotMatch(app, /setInterval\([^)]*refreshWorkspaces/);
});

test("live events apply the usage payload instantly with no revision gating", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  // Every event is applied the instant it arrives — the revision de-dup guard is gone.
  assert.doesNotMatch(app, /revision <= lastRevision/);
  assert.doesNotMatch(app, /let lastRevision/);
  // The pushed usage payload (tokens/cost/tools) is stored per run and shown in Breakdown.
  assert.match(app, /state\.liveUsage\[event\.run_id\]\s*=\s*event\.usage/);
  const pulseSource = app.slice(app.indexOf("function renderRunPulse"), app.indexOf("function svgElement"));
  assert.doesNotMatch(pulseSource, /liveUsageNodes|liveToolsNode|attempt/);
  assert.match(app, /function liveBreakdownMetric/);
  // High-frequency tool_progress stays out of the discrete event feed and inspector refetch.
  assert.match(app, /\["tool_progress", "usage_progress"\]\.includes\(event\.type\)/);
  // Initial sync/reconnect restores the last pushed snapshot and Breakdown prefers it while live.
  assert.match(app, /state\.liveUsage\[run\.run_id\]\s*=\s*run\.live_usage/);
  assert.match(app, /const usage = liveUsage \|\| breakdown\.cumulative_usage/);
  assert.match(app, /liveTotalTimeMetric\(selectedRun, breakdown\.started_at/);
  assert.match(app, /toolCallMetric\(selectedRun, executions, liveUsage\)/);
  assert.match(app, /execution\.verdict != null/);
  assert.match(app, /execution_tool_calls_total/);
  assert.match(app, /usage\.phase_usages\?\.\[phase\]\?\.tool_calls_total/);
  assert.match(app, /dataset\.liveRunStarted = String\(startedAt\)/);
  assert.match(app, /\[data-live-run-started\]/);
  // High-rate progress mutates only bound text/tool nodes: it must not rebuild gauges or restart
  // the orbit/ripple animations through the global render path.
  assert.match(app, /if \(isProgress\) \{\s*updateProgressRegions\(event\.run_id\);\s*return;/);
  assert.match(app, /data-live-usage-run-id|liveUsageRunId/);
  assert.doesNotMatch(app, /data-live-tools-run-id|liveToolsRunId/);
  assert.match(app, /data-live-phase-started|livePhaseStarted/);
  assert.match(app, /data-live-phase-tokens|livePhaseTokens/);
  // Active phase rows consume the backend-attributed snapshot directly. A page refresh must not
  // invent a new browser-local baseline and reset the row to zero.
  assert.match(app, /usage\.phase_usages\?\.\[row\.dataset\.livePhaseId\] \|\| usage\.phase_usage \|\| \{\}/);
  assert.match(app, /liveRun\?\.active_phases\?\.\[item\.phase\]/);
  assert.match(app, /row\.dataset\.livePhaseId = item\.phase/);
  assert.match(app, /row\.dataset\.livePhasePriorTokens = String/);
  assert.match(app, /phaseUsage\.total_tokens.*priorTokens/s);
  assert.match(app, /phase-row-nested/);
  assert.match(app, /phase_graph: selectedRun\?\.phase_graph \|\| state\.runDetail\?\.phase_graph/);
  assert.match(app, /breakdownTable\(executions, false, tableRun\)/);
  assert.match(app, /row\.classList\.add\("phase-row-active"\)/);
  assert.doesNotMatch(app, /item\.call_number/);
  assert.doesNotMatch(app, /phaseUsageBaseline|phaseTotal\s*=.*-\s*baseline/);
});

test("terminal history without a legacy verdict is not presented as active", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const history = app.slice(app.indexOf("function historyTable"), app.indexOf("function renderBreakdown"));
  assert.match(history, /item\.verdict \|\| "incomplete"/);
  assert.doesNotMatch(history, /item\.verdict \|\| "active"/);
  const breakdown = app.slice(app.indexOf("function breakdownTable"));
  assert.match(breakdown, /item\.verdict \|\| \(isLive \? "active" : "incomplete"\)/);
  assert.doesNotMatch(breakdown, /item\.verdict \|\| "active"/);
});

test("overview shows lifetime stats while run detail keeps the phase graph", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const styles = await readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8");
  assert.doesNotMatch(app, /phaseGraphOpen|graph-toggle|aria-expanded/);
  const overview = app.slice(app.indexOf("function renderOverview({"), app.indexOf("function eventLine"));
  assert.match(overview, /append\(fragment, renderOverviewRunPulse\(\)\);[\s\S]*append\(fragment, renderLifetimeStats\(\)\)/);
  assert.doesNotMatch(overview, /renderPhaseGraphPanel/);
  assert.match(overview, /No model usage has been recorded yet/);
  assert.match(overview, /outcome-ring/);
  assert.match(overview, /model-stat-fill/);
  assert.match(overview, /panel\("Statistics"/);
  assert.match(overview, /statCard\("Token Usage"/);
  const totals = overview.slice(overview.indexOf('statCard("Totals"'), overview.indexOf('statCard("Recent Run Trends"'));
  assert.match(totals, /compactLifetimeMetric\("Model loads", stats\.model_loads\)/);
  assert.match(totals, /compactLifetimeMetric\("Model load time", formatDuration\(stats\.model_load_duration_s\)\)/);
  assert.match(overview, /statCard\("Recent Run Trends"/);
  assert.match(overview, /statCard\("Phase Time"/);
  assert.match(overview, /statCard\("Queue"/);
  assert.doesNotMatch(overview, /panel\("System Status"/);
  assert.doesNotMatch(overview, /panel\("Execution Queue"/);
  assert.match(overview, /sparkline\("Tokens per run"/);
  assert.doesNotMatch(overview, /lifetime-headline/);
  assert.match(overview, /state\.projectQueue\?\.batches/);
  const recentRuns = app.slice(app.indexOf("function renderRecentRuns"), app.indexOf("function runsTable"));
  assert.match(recentRuns, /runsTable\(state\.overviewRuns\)/);
  assert.match(recentRuns, /Page \$\{page\} · 25 runs/);
  assert.match(app, /renderRunPulse\(state\.runDetail[\s\S]*?renderPhaseGraphPanel\(state\.runDetail/);
  const breakdown = app.slice(app.indexOf("function renderBreakdown"), app.indexOf("function renderArtifacts"));
  assert.doesNotMatch(breakdown, /metric\("Model load time"/);
  assert.match(app, /Configured phase routes and traversal counts/);
  assert.match(app, /marker-end/);
  assert.match(app, /phase-node-duration/);
  assert.match(app, /duration\.dataset\.livePhaseStarted/);
  assert.match(app, /duration\.dataset\.livePhaseBase/);
  assert.match(app, /const currentModel = phase === run\.phase \? run\.model : null/);
  assert.match(app, /currentModel \|\| modelLoad\?\.model \|\| null/);
  assert.match(app, /MODEL · NOT REPORTED/);
  assert.match(styles, /\.current-phase-identity strong \{[^}]*color: var\(--cyan\)/);
  assert.match(styles, /\.current-phase-duration \{ color: var\(--violet\); \}/);
  assert.match(styles, /\.current-phase-tokens \{ color: var\(--magenta\); \}/);
  assert.match(styles, /\.current-phase-tools \{ color: var\(--green\); \}/);
  assert.match(app, /"text-anchor": "middle"/);
  assert.doesNotMatch(app, /class: "phase-node-label"/);
});

test("pulse, graph, and top-level section spacing keep their compact structural contract", async () => {
  const app = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const styles = await readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8");
  assert.match(styles, /main \{[^}]*display: grid;[^}]*gap: 1rem;/);
  assert.match(styles, /\.scanlines \{[^}]*z-index: 20;/);
  assert.match(styles, /\.panel \{[^}]*z-index: 21;/);
  assert.doesNotMatch(styles, /\.page-header \{[^}]*margin-bottom/);
  assert.match(styles, /\.run-pulse \{[^}]*grid-template-columns: 116px minmax\(0, 1fr\);/);
  assert.match(styles, /\.run-pulse-stack \{[^}]*display: grid;[^}]*gap: 1rem;/);
  assert.match(styles, /\.pulse-copy \.notice \{[^}]*display: block;[^}]*margin-top: \.65rem;[^}]*line-height: 1\.4;/);
  assert.match(styles, /\.resource-panel \{[^}]*border-color:/);
  assert.match(styles, /\.resource-gauge \{[^}]*grid-template-rows: auto 1fr;/);
  assert.match(styles, /\.gauge-horizontal-well \{[^}]*height: 22px;/);
  assert.match(styles, /\.gauge-horizontal-fill \{[^}]*clip-path: inset\(0 calc\(100% - var\(--bar-load\) \* 1%\) 0 0\)/);
  assert.match(styles, /\.gauge-bars \{[^}]*display: grid;/);
  assert.match(styles, /\.gauge-value \{[^}]*place-items: center;/);
  assert.match(app, /memoryKind = key === "cpu" \? "RAM" : "VRAM"/);
  assert.match(app, /replace\(\/\^NVIDIA\\s\+\(\?:GeForce\\s\+\)\?\/i, ""\)/);
  assert.match(app, /Number\(reading\.index\) \+ 1/);
  assert.match(app, /\(memoryUsed \/ memoryTotal\) \* 100/);
  assert.match(app, /formatMemoryGb\(memoryUsed\).*formatMemoryGb\(memoryTotal\).*GB/s);
  assert.match(styles, /\.phase-graph-scroll \{[^}]*overflow-x: auto/);
  assert.match(styles, /\.phase-edge path \{[^}]*fill: none;[^}]*stroke:/);
  assert.match(styles, /\.phase-node\[data-state="active"\] rect \{[^}]*phase-node-pulse/);
  assert.match(styles, /--phase-node-fill: #080515/);
  assert.match(styles, /\.phase-node rect \{ fill: var\(--phase-node-fill\)/);
  assert.match(styles, /\.phase-self-check-loop rect \{ fill: var\(--phase-node-fill\)/);
  assert.match(styles, /\.breakdown-metrics \{[^}]*grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)[^}]*margin-bottom: 1rem/);
  assert.match(styles, /\.breakdown-metrics \.metric \{[^}]*min-height: 0/);
  assert.match(styles, /\.phase-breakdown-table \.phase-row-active td \{[^}]*background:/);
  assert.match(styles, /\.phase-breakdown-table tbody td \{ text-transform: uppercase; \}/);
  assert.doesNotMatch(styles, /\.phase-breakdown-table th \{ text-align: center; \}/);
  assert.match(styles, /\.phase-breakdown-table \.phase-row-reason \{ text-transform: none; \}/);
});

test("concurrency groups cluster lanes and prefer the backend label", () => {
  const nodes = [
    { id: "review_impl.architecture", group: "review_impl" },
    { id: "review_impl.correctness", group: "review_impl" },
    { id: "research_requirements", group: "research" },
    { id: "research_technical", group: "research" },
    { id: "plan", group: null },
  ];
  const groups = concurrencyGroups(nodes, [
    { id: "review_impl", label: "implementation audits" },
  ]);

  assert.deepEqual(groups.map((group) => [group.id, group.label, group.members.length]), [
    ["review_impl", "implementation audits", 2],
    // No backend label for this run's plan, so the group id stands in.
    ["research", "research", 2],
  ]);
});

test("a solitary lane gets no container", () => {
  const groups = concurrencyGroups([{ id: "only.one", group: "only" }], []);
  assert.deepEqual(groups, []);
});

test("grouped lanes display their own name, not the shared prefix", () => {
  assert.equal(laneDisplayId({ id: "review_impl.architecture", group: "review_impl" }), "architecture");
  assert.equal(laneDisplayId({ id: "research_requirements", group: "research" }), "requirements");
  assert.equal(laneDisplayId({ id: "plan", group: null }), "plan");
  // A lane whose id is exactly the group name keeps its id rather than becoming empty.
  assert.equal(laneDisplayId({ id: "research", group: "research" }), "research");
});

test("group boxes enclose their lanes and leave room for the caption", () => {
  const nodes = [
    { id: "a", group: "g", x: 100, y: 60, width: 80, height: 64, selfCheck: false, selfFixRan: false },
    { id: "b", group: "g", x: 100, y: 160, width: 80, height: 64, selfCheck: false, selfFixRan: false },
  ];
  const [box] = groupBoxes([{ id: "g", label: "group", members: ["a", "b"] }], nodes);

  assert.ok(box.x < 100 && box.x + box.width > 180, "box spans the lanes horizontally");
  assert.ok(box.y < 60, "box top clears the first lane");
  assert.ok(box.y + box.height > 224, "box bottom clears the last lane");
  assert.ok(box.labelY > box.y && box.labelY < 60, "caption sits above the lanes, inside the box");
});

test("grouped lanes form a bottom-anchored quarter-width staircase", () => {
  const run = {
    phase_graph: {
      nodes: [
        { id: "g.top", label: "Top", order: 0, column: 0, lane: 0, group: "g" },
        { id: "g.middle", label: "Middle", order: 1, column: 0, lane: 1, group: "g" },
        { id: "g.bottom", label: "Bottom", order: 2, column: 0, lane: 2, group: "g" },
        { id: "next", label: "Next", order: 3, column: 1, lane: 0 },
      ],
      edges: [],
    },
  };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));
  const top = layout.nodes.find((node) => node.id === "g.top");
  const middle = layout.nodes.find((node) => node.id === "g.middle");
  const bottom = layout.nodes.find((node) => node.id === "g.bottom");
  const next = layout.nodes.find((node) => node.id === "next");
  const [box] = layout.groups;

  assert.equal(middle.x - top.x, top.width / 4, "middle sits one quarter-width right of the top");
  assert.equal(bottom.x - middle.x, middle.width / 4, "bottom anchors the staircase on the right");
  assert.ok(box.x < top.x, "container encloses the top lane's left edge");
  assert.ok(box.x + box.width > bottom.x + bottom.width, "container encloses the bottom lane's right edge");
  assert.ok(box.x + box.width < next.x, "expanded container clears the following column");
});

test("staggered grouped lanes separate retry return legs", () => {
  const run = {
    phase_graph: {
      nodes: [
        { id: "g.top", label: "Top", order: 0, column: 0, lane: 0, group: "g" },
        { id: "g.middle", label: "Middle", order: 1, column: 0, lane: 1, group: "g" },
        { id: "g.bottom", label: "Bottom", order: 2, column: 0, lane: 2, group: "g" },
        { id: "gate", label: "Gate", order: 3, column: 1, lane: 0 },
      ],
      edges: [
        { source: "gate", target: "g.top", kinds: ["retry"] },
        { source: "gate", target: "g.middle", kinds: ["retry"] },
        { source: "gate", target: "g.bottom", kinds: ["retry"] },
      ],
    },
    phase_route_counts: {
      "gate->g.top": 1,
      "gate->g.middle": 1,
      "gate->g.bottom": 1,
    },
  };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));
  const returnLegs = layout.edges.map((edge) => (
    Number(edge.path.match(/H ([\d.]+) V [\d.]+ H [\d.]+$/)[1])
  ));
  const lanes = Object.fromEntries(layout.edges.map((edge) => [edge.target, edge.lane]));

  assert.equal(new Set(returnLegs).size, 3, "each retry returns on its lane's own vertical leg");
  assert.deepEqual(lanes, {
    "g.top": 3,
    "g.middle": 2,
    "g.bottom": 1,
  }, "the innermost retry reaches the bottom lane and outer retries progress upward");
});

test("group boxes stay inside the viewBox when lanes carry self-check decorations", () => {
  // Regression: a grouped lane that also draws a self-check needs room for the decoration AND the
  // container caption. Taking the max of the two extents put the box at a negative y, so the
  // container clipped out of the SVG.
  const run = {
    phase_graph: {
      nodes: [
        { id: "research_requirements", label: "requirements", order: 0, column: 0, lane: 0, group: "research", self_check: true },
        { id: "research_architecture", label: "architecture", order: 1, column: 0, lane: 1, group: "research", self_check: true },
        { id: "research_technical", label: "technical", order: 2, column: 0, lane: 2, group: "research", self_check: true },
        { id: "plan", label: "plan", order: 3, column: 1, lane: 0 },
      ],
      edges: [{ source: "research_requirements", target: "plan", kinds: ["normal"] }],
    },
  };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));
  const [box] = layout.groups;

  assert.ok(box.y >= 0, `group box top ${box.y} must not be negative`);
  assert.ok(box.y + box.height <= layout.height, "group box bottom must fit the viewBox");
  assert.ok(box.labelY > box.y, "caption sits below the box top edge");
});

test("group boxes expose the member count the tooltip renders", () => {
  // Regression: groupBoxes omitted `members`, and app.mjs read `group.members.length` for the
  // tooltip. That threw during render and blanked the entire phase graph.
  const nodes = [
    { id: "g.a", group: "g", x: 0, y: 0, width: 80, height: 64, selfCheck: false, selfFixRan: false },
    { id: "g.b", group: "g", x: 0, y: 100, width: 80, height: 64, selfCheck: false, selfFixRan: false },
  ];
  const [box] = groupBoxes([{ id: "g", label: "group", members: ["g.a", "g.b"] }], nodes);

  assert.equal(box.memberCount, 2);
  assert.ok(!`${box.label}: ${box.memberCount} concurrent phases`.includes("undefined"));
});

test("reserved decoration space always covers the loop actually drawn", () => {
  // The layout reserves SELF_DECORATION_EXTENT while app.mjs draws selfLoopLayout(). If those two
  // ever drift the badge clips silently, so the reservation is derived from the drawn geometry and
  // this asserts the invariant rather than any particular number.
  for (const [label, below] of [["SELF CHECK", false], ["SELF FIX", true]]) {
    const g = selfLoopLayout(label, 140, 64, { below });
    const overshoot = below
      ? g.badge.y + g.badge.height - 64
      : -g.badge.y;
    assert.ok(overshoot <= g.extent, `${label} badge (${overshoot}) exceeds reserve (${g.extent})`);
    assert.ok(Math.abs(g.captionY - (below ? 64 : 0)) <= g.extent, `${label} caption outside reserve`);
  }
});

test("self-loop badge scales with its label instead of a fixed width", () => {
  const short = selfLoopLayout("AB", 200, 64);
  const long = selfLoopLayout("SELF CHECK", 200, 64);

  assert.ok(long.badge.width > short.badge.width, "longer caption gets a wider badge");
  assert.equal(short.badge.x + short.badge.width / 2, 100, "badge stays centred on the node");
  assert.equal(long.badge.x + long.badge.width / 2, 100);
});

test("self-loop path returns to the node edge so the arrowhead lands on it", () => {
  const above = selfLoopLayout("SELF CHECK", 140, 64);
  const below = selfLoopLayout("SELF FIX", 140, 64, { below: true });

  assert.ok(above.path.endsWith("V -2"), `above loop must return to the top edge: ${above.path}`);
  assert.ok(below.path.endsWith("V 66"), `below loop must return to the bottom edge: ${below.path}`);
  // Both legs are inset symmetrically from the node's sides.
  assert.match(above.path, /^M 120 0 V -22 H 20 /);
});

test("node text rows are derived from node height, not hardcoded per call site", () => {
  const tall = nodeTextRows(100);
  const standard = nodeTextRows(64);

  assert.equal(tall.topY, standard.topY, "top row is a fixed inset from the top edge");
  assert.equal(tall.bottomY - 100, standard.bottomY - 64, "bottom row tracks the bottom edge");
  assert.ok(standard.topY < standard.bottomY);
});

test("retry loops share a lane when they do not overlap, and stack only when they do", () => {
  const nodes = Array.from({ length: 6 }, (_, i) => ({
    id: `n${i}`, label: `N${i}`, order: i, column: i, lane: 0,
  }));
  const edges = [
    ...nodes.slice(0, -1).map((node, i) => ({ source: node.id, target: `n${i + 1}`, kinds: ["normal"] })),
    { source: "n1", target: "n0", kinds: ["retry"] },   // short, far left
    { source: "n5", target: "n4", kinds: ["retry"] },   // short, far right — disjoint from the above
    { source: "n5", target: "n0", kinds: ["retry"] },   // long, spans and overlaps both
  ];
  const counts = { "n1->n0": 1, "n5->n4": 1, "n5->n0": 1 };
  const layout = layoutPhaseGraph(normalizePhaseGraph({
    phase_graph: { nodes, edges }, phase_route_counts: counts,
  }));
  const lane = (key) => layout.edges.find((edge) => edge.key === key).lane;
  const depth = (key) => Number(layout.edges.find((edge) => edge.key === key)
    .path.match(/V ([\d.]+) H/)[1]);

  // Disjoint spans cost no extra depth.
  assert.equal(lane("n1->n0"), lane("n5->n4"));
  assert.equal(depth("n1->n0"), depth("n5->n4"));
  // The long route overlaps both, so it is pushed outward rather than drawn over them.
  assert.ok(lane("n5->n0") > lane("n1->n0"), "overlapping route takes a deeper lane");
  assert.ok(depth("n5->n0") > depth("n1->n0"));
  // The shallowest lane still clears everything drawn above it.
  assert.ok(depth("n1->n0") > layout.contentBottom, "first lane must clear all content");
  assert.ok(layout.height > depth("n5->n0"), "canvas contains the deepest lane");
});

test("retry depth is paid for overlap, not for loop count", () => {
  const build = (retryCount) => {
    const nodes = Array.from({ length: retryCount * 2 }, (_, i) => ({
      id: `n${i}`, label: `N${i}`, order: i, column: i, lane: 0,
    }));
    const edges = nodes.slice(0, -1).map((node, i) => ({
      source: node.id, target: `n${i + 1}`, kinds: ["normal"],
    }));
    const counts = {};
    // Disjoint back-edges: n1->n0, n3->n2, n5->n4 … none overlap.
    for (let i = 1; i < nodes.length; i += 2) {
      edges.push({ source: `n${i}`, target: `n${i - 1}`, kinds: ["retry"] });
      counts[`n${i}->n${i - 1}`] = 1;
    }
    return layoutPhaseGraph(normalizePhaseGraph({
      phase_graph: { nodes, edges }, phase_route_counts: counts,
    }));
  };
  const one = build(1);
  const three = build(3);

  assert.ok(three.edges.filter((edge) => edge.lane > 0).length === 3);
  assert.equal(new Set(three.edges.filter((e) => e.lane > 0).map((e) => e.lane)).size, 1);
  assert.equal(one.height - one.contentBottom, three.height - three.contentBottom);
});

test("a container grows when one of its stacked lanes spawns a self-fix", () => {
  const run = {
    phase_graph: {
      nodes: [
        { id: "g.a", label: "A", order: 0, column: 0, lane: 0, group: "g", self_fix: true },
        { id: "g.b", label: "B", order: 1, column: 0, lane: 1, group: "g", self_fix: true },
      ],
      edges: [],
    },
  };
  const before = layoutPhaseGraph(normalizePhaseGraph(run));
  // The lower lane repairs itself mid-run; the container must take in its loop, not clip it.
  const after = layoutPhaseGraph(normalizePhaseGraph({ ...run, self_fixes: { "g.b": "completed" } }));

  const [boxBefore] = before.groups;
  const [boxAfter] = after.groups;
  const lane = after.nodes.find((node) => node.id === "g.b");
  const loop = selfLoopLayout("SELF FIX", lane.width, lane.height, { below: true });

  assert.ok(boxAfter.height > boxBefore.height, "container must grow for the new self-fix");
  assert.ok(
    boxAfter.y + boxAfter.height >= lane.y + loop.badge.y + loop.badge.height,
    "container's lower edge must enclose the self-fix badge",
  );
  assert.ok(after.height >= boxAfter.y + boxAfter.height, "canvas must contain the grown container");
});

test("a container grows upward for a stacked lane's self-check", () => {
  const base = {
    phase_graph: {
      nodes: [
        { id: "g.a", label: "A", order: 0, column: 0, lane: 0, group: "g" },
        { id: "g.b", label: "B", order: 1, column: 0, lane: 1, group: "g" },
      ],
      edges: [],
    },
  };
  const plain = layoutPhaseGraph(normalizePhaseGraph(base));
  const checked = structuredClone(base);
  checked.phase_graph.nodes[0].self_check = true;
  const withCheck = layoutPhaseGraph(normalizePhaseGraph(checked));

  assert.ok(withCheck.groups[0].height > plain.groups[0].height);
  assert.ok(withCheck.groups[0].y >= 0, "container must not clip off the top of the canvas");
});

test("routes into a container share one trunk that splits inside the border", () => {
  const run = {
    phase_graph: {
      nodes: [
        { id: "src", label: "Src", order: 0, column: 0, lane: 0 },
        { id: "g.a", label: "A", order: 1, column: 1, lane: 0, group: "g" },
        { id: "g.b", label: "B", order: 2, column: 1, lane: 1, group: "g" },
      ],
      edges: [
        { source: "src", target: "g.a", kinds: ["normal"] },
        { source: "src", target: "g.b", kinds: ["normal"] },
      ],
    },
  };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));
  const [box] = layout.groups;
  const bends = layout.edges.map((edge) => Number(edge.path.match(/H ([\d.]+) V/)[1]));

  assert.equal(new Set(bends).size, 1, "both routes bend at the same trunk");
  assert.ok(bends[0] > box.x, "the split happens inside the dashed border, not on it");
  assert.ok(bends[0] < layout.nodes.find((n) => n.id === "g.a").x, "trunk sits left of the lanes");
});

test("a column holding a container is given room for its wider border", () => {
  const nodes = (grouped) => ([
    { id: "a", label: "A", order: 0, column: 0, lane: 0 },
    { id: "b", label: "B", order: 1, column: 1, lane: 0, ...(grouped ? { group: "g" } : {}) },
    { id: "c", label: "C", order: 2, column: 1, lane: 1, ...(grouped ? { group: "g" } : {}) },
    { id: "d", label: "D", order: 3, column: 2, lane: 0 },
  ]);
  const plain = layoutPhaseGraph(normalizePhaseGraph({ phase_graph: { nodes: nodes(false), edges: [] } }));
  const grouped = layoutPhaseGraph(normalizePhaseGraph({ phase_graph: { nodes: nodes(true), edges: [] } }));
  const [box] = grouped.groups;
  const left = grouped.nodes.find((n) => n.id === "a");
  const right = grouped.nodes.find((n) => n.id === "d");

  assert.ok(grouped.width > plain.width, "grouped column claims extra horizontal room");
  assert.ok(box.x > left.x + left.width, "container clears the previous column");
  assert.ok(box.x + box.width < right.x, "container clears the next column");
});

test("routes leave a container as close to their block as they arrive", () => {
  const run = {
    phase_graph: {
      nodes: [
        { id: "src", label: "Src", order: 0, column: 0, lane: 0 },
        { id: "g.a", label: "A", order: 1, column: 1, lane: 0, group: "g" },
        { id: "g.b", label: "B", order: 2, column: 1, lane: 1, group: "g" },
        { id: "dst", label: "Dst", order: 3, column: 2, lane: 0 },
      ],
      edges: [
        { source: "src", target: "g.a", kinds: ["normal"] },
        { source: "src", target: "g.b", kinds: ["normal"] },
        { source: "g.a", target: "dst", kinds: ["normal"] },
        { source: "g.b", target: "dst", kinds: ["normal"] },
      ],
    },
  };
  const layout = layoutPhaseGraph(normalizePhaseGraph(run));
  const [box] = layout.groups;
  const lanes = layout.nodes.filter((node) => node.group === "g");
  const laneLeft = Math.min(...lanes.map((node) => node.x));
  const laneRight = Math.max(...lanes.map((node) => node.x + node.width));
  const bendsOf = (predicate) => [...new Set(layout.edges.filter(predicate)
    .map((edge) => Number(edge.path.match(/H ([\d.]+) V/)[1])))];

  const inbound = bendsOf((edge) => edge.target.startsWith("g."));
  const outbound = bendsOf((edge) => edge.source.startsWith("g."));

  assert.equal(inbound.length, 1, "arriving routes share one trunk");
  assert.equal(outbound.length, 1, "departing routes share one trunk");
  // Outbound used to run to the middle of the gap, far past the border; it must now mirror inbound.
  assert.equal(laneLeft - inbound[0], outbound[0] - laneRight, "trunks are equidistant from the lanes");
  assert.ok(outbound[0] < box.x + box.width, "departing bend stays inside the dashed border");
  assert.ok(inbound[0] > box.x, "arriving bend stays inside the dashed border");
});

test("a container reports the same states its lanes do", () => {
  const group = { id: "g" };
  const nodes = [{ id: "g.a", group: "g" }, { id: "g.b", group: "g" }, { id: "solo", group: null }];
  const state = (a, b) => groupState(group, nodes, { "g.a": a, "g.b": b, solo: "failed" });

  assert.equal(state("unvisited", "unvisited"), "unvisited");
  assert.equal(state("completed", "unvisited"), "unvisited", "partial completion is not green");
  assert.equal(state("completed", "completed"), "completed");
  assert.equal(state("failed", "completed"), "failed", "any failed lane fails the container");
  // Still running: the container has not settled, so it stays active even beside a failed lane.
  assert.equal(state("active", "completed"), "active");
  assert.equal(state("active", "failed"), "active");
  // A lane outside the group must not influence it.
  assert.equal(groupState({ id: "other" }, nodes, { solo: "failed" }), "unvisited");
});

test("the models route resolves instead of falling back to overview", () => {
  // parseRoute has an allowlist; a nav entry without a matching entry silently lands on overview.
  assert.deepEqual(parseRoute("#/models"), { section: "models", id: null });
});

test("model switching calls the API with the exact switch contract", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), method: options.method || "GET", body: options.body });
    return new Response(JSON.stringify({ status: "switching" }), {
      status: 202,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await QuillApi.switchModel("Qwen3.6_35B_A3B_NVFP4");
    await QuillApi.switchModel("Qwen3.6_35B_A3B_NVFP4", true);
    await QuillApi.unloadModel("Qwen3.6_35B_A3B_NVFP4");
    await QuillApi.unloadModel("Qwen3.6_35B_A3B_NVFP4", true);
    await QuillApi.refreshModels();

    assert.equal(calls[0].method, "POST");
    assert.ok(calls[0].url.endsWith("/models/switch"));
    assert.deepEqual(JSON.parse(calls[0].body), {
      model_id: "Qwen3.6_35B_A3B_NVFP4",
      force: false,
    });
    // Forcing is opt-in: a plain load must never send force, or it would stop a run's model.
    assert.equal(JSON.parse(calls[1].body).force, true);
    assert.ok(calls[2].url.endsWith("/models/unload"));
    assert.deepEqual(JSON.parse(calls[2].body), {
      model_id: "Qwen3.6_35B_A3B_NVFP4",
      force: false,
    });
    assert.equal(JSON.parse(calls[3].body).force, true);
    // Re-scanning is explicit; the default read must stay cached.
    assert.ok(calls[4].url.includes("refresh=true"));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("the models table owns load and unload controls while resident identity stays compact", async () => {
  const src = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(src, /panel\("Load a model"\)/);
  assert.doesNotMatch(src, /"KV", "State"/);
  assert.match(src, /"KV", "Action"/);
  assert.match(src, /button\("Load", "success small"/);
  assert.match(src, /button\("Unload", "danger small"/);
  assert.match(src, /className = "badge resident-model"|"badge resident-model"/);
  assert.match(src, /formatNumber\(residentEntry\.max_model_len\).*ctx/);
  assert.doesNotMatch(src, /Switch: /);
  assert.match(src, /refreshModels\(\{ quiet: true \}\)/);
});

test("the models tab is reachable from the primary navigation", async () => {
  const markup = await readFile(new URL("../../quill_api/web/index.html", import.meta.url), "utf8");
  assert.match(markup, /href="#\/models" data-route="models"/);
});

test("queue navigation, grouped selection, SSE, and overview snapshot are wired", async () => {
  const [markup, app, styles] = await Promise.all([
    readFile(new URL("../../quill_api/web/index.html", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../quill_api/web/styles.css", import.meta.url), "utf8"),
  ]);
  assert.match(markup, /data-route="runs">Runs<\/a>\s*<a href="#\/queue" data-route="queue">Queue<\/a>\s*<a href="#\/workspaces"/);
  assert.match(app, /state\.route\.section === "queue"\) main\.append\(renderQueue\(\)\)/);
  assert.match(app, /queueCapableRepositories\(state\.github\.repositories\)/);
  assert.match(app, /groupSelectionState\(group, state\.queuePage\.selected\)/);
  assert.match(app, /selector\.indeterminate = selection\.indeterminate/);
  assert.match(app, /if \(!ticket\.selectable\) continue/);
  assert.match(app, /"Standalone tickets"/);
  assert.match(app, /Add To Queue/);
  assert.match(app, /event\.type === "project_queue_updated"/);
  assert.match(app, /state\.projectQueue = event\.project_queue \|\| state\.projectQueue/);
  assert.match(app, /dataset\.liveRegion = "project-queue-order"/);
  assert.doesNotMatch(app, /Skip ticket|Advance queue/);

  assert.match(app, /function renderOverviewRunPulse/);
  assert.match(app, /function renderCurrentPhase/);
  assert.match(app, /state\.runTab = "breakdown"/);
  assert.match(app, /Object\.entries\(run\.active_phases \|\| \{\}\)/);
  assert.match(app, /phaseUsage\.context_window_tokens \?\? phaseUsage\.total_tokens/);
  assert.match(styles, /\.overview-run-row \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(styles, /\.stats-trends \{ grid-column: 1 \/ -1; \}/);
  assert.match(styles, /\.stats-project-queue \{ grid-column: span 3; \}/);
  assert.match(styles, /\.trend-grid \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.doesNotMatch(app, /sparkline-axis-title|Completed runs \(oldest → newest\)/);
  assert.match(app, /left: sparklineLeftMargin\(yTicks\.map/);
  assert.match(app, /\[plot\.left, "Run 1", "start"\]/);
  assert.match(app, /\[plot\.right, `Run \$\{values\.length\}`, "end"\]/);
  assert.match(app, /linearTrend\(values\)/);
  assert.match(styles, /\.sparkline-trend \{[^}]*stroke-dasharray:/);
});

test("startup loads data for the route it lands on", async () => {
  // A hard refresh onto #/models used to render once with no data and never fetch any: startup
  // listed only `runs` and `memories`, so every other route waited for the user to navigate away
  // and back before anything appeared.
  const src = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const tail = src.slice(src.indexOf("if (!location.hash) history.replaceState"));
  assert.match(tail, /^handleRoute\(\);$/m, "startup must run the per-route data path");
  assert.doesNotMatch(
    tail,
    /if \(state\.route\.section === "memories"\) refreshMemories/,
    "per-route loading belongs in handleRoute, not duplicated at startup",
  );

  const route = src.slice(src.indexOf("async function handleRoute()"), src.indexOf("function updateElapsed()"));
  for (const section of ["queue", "workspaces", "memories", "personas", "skills", "models", "settings"]) {
    assert.ok(route.includes(`=== "${section}"`), `handleRoute has no branch for ${section}`);
  }
});

test("system data repaints the routes that render it", async () => {
  const src = await readFile(new URL("../../quill_api/web/app.mjs", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("async function refreshSystem("), src.indexOf("async function refreshRuns("));
  assert.match(body, /\["models", "settings", "api"\]\.includes\(state\.route\.section\)[\s\S]{0,60}render\(\)/);
});
