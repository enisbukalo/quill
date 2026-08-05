import { ApiError, QuillApi } from "./api.mjs";
import {
  branchName,
  canAnswerRun,
  canStopRun,
  chooseSelection,
  diagnosticSummary,
  formatBytes,
  formatDuration,
  formatMemoryGb,
  formatMoney,
  formatNumber,
  formatPercent,
  formatTemperature,
  formatTime,
  liveRunLabel,
  parseRoute,
  preferredWorkType,
  pruneQueueSelection,
  queueCapableRepositories,
  groupSelectionState,
  runElapsed,
  safeExternalUrl,
  statusTone,
  validCatalogName,
  validReason,
} from "./format.mjs";
import {
  centeredPhaseScroll,
  contractEdgeLabel,
  edgeLabelPosition,
  formatPhaseDuration,
  layoutPhaseGraph,
  normalizePhaseGraph,
  phaseGraphStructureSignature,
  phaseEdgeState,
  phaseGraphMetrics,
  groupState,
  nodeTextRows,
  phaseNodeStates,
  selfLoopLayout,
} from "./phase-graph.mjs";
import { reconcileModelOverrides } from "./run-model-overrides.mjs";
import { linearTrend, sparklineLeftMargin } from "./trends.mjs";

const main = document.querySelector("#main");
const toastRegion = document.querySelector("#toast-region");
const connectionStatus = document.querySelector("#connection-status");
const connectionLabel = document.querySelector("#connection-label");
const lastUpdated = document.querySelector("#last-updated");
const footerVersion = document.querySelector("#footer-version");

const state = {
  route: parseRoute(location.hash),
  connection: "connecting",
  health: null,
  version: null,
  models: null,
  telemetry: null,
  telemetrySettings: {
    cpu_temperature_min_c: 20,
    cpu_temperature_max_c: 70,
    gpu_temperature_min_c: 20,
    gpu_temperature_max_c: 80,
  },
  stats: null,
  init: null,
  queue: { active: null, queued: [], depth: 0 },
  projectQueue: { batches: [], depth: 0 },
  queuePage: { repo: "", groups: [], selected: new Set(), result: null },
  runs: [],
  overviewRuns: [],
  overviewRunPage: { limit: 25, offset: 0, hasMore: false },
  runFacets: [],
  selectedRunIds: new Set(),
  github: { login: "", repositories: [], repo: "", allIssues: [], issues: [], allWorkTypes: [], work_types: [], workflows: [], models: [], excludedIssueLabels: [], updateTarget: null },
  runDraft: {
    repo: "",
    work_type: "",
    branch: "",
    ticket: "",
    mode: "create",
    workflow: "ticket",
    override_models: false,
    model_overrides: {},
  },
  runFilters: { repo: "", ticket: "", status: "", offset: 0 },
  runPage: { limit: 200, offset: 0, hasMore: false },
  //: Latest live per-phase usage snapshot pushed during a run, keyed by run_id: tokens, cost, tools.
  liveUsage: {},
  livePhase: {},
  phaseStartedAt: {},
  phaseGraphScroll: {},
  phaseGraphFocus: {},
  workspaces: { list: [], repo: "", branches: [], current: null, branch: "", result: null },
  memories: [],
  memoryArchivedEvents: 0,
  memoryRepo: "",
  selectedMemoryIds: new Set(),
  runDetail: null,
  restartOptions: null,
  breakdown: null,
  artifacts: [],
  artifact: null,
  runTab: "status",
  decisionDraft: "",
  pendingInspectorRefresh: false,
  recentEvents: [],
  personas: [],
  personaRoot: "",
  persona: null,
  personaCreating: false,
  skills: [],
  skillRoot: "",
  skill: null,
  skillCreating: false,
  skillFile: null,
  skillFileCreating: false,
  catalogSearch: "",
  editorDirty: false,
  loading: new Set(),
  errors: {},
  lastRefresh: null,
};

const controllers = new Map();
let eventSource = null;
let telemetrySource = null;
let detailRefreshTimer = null;
let lastHash = location.hash || "#/overview";
let openSelect = null;
let lastModelOperationStatus = "idle";

function element(tag, className = "", text = null) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== null && text !== undefined) result.textContent = String(text);
  return result;
}

function append(parent, ...children) {
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) parent.append(child);
  }
  return parent;
}

function heading(eyebrow, title, subtitle, actions = null) {
  const wrap = element("div", "page-header");
  const copy = element("div");
  append(copy, element("p", "eyebrow", eyebrow), element("h1", "", title));
  if (subtitle) append(copy, element("p", "subtitle", subtitle));
  append(wrap, copy, actions);
  return wrap;
}

function panel(title = null, className = "") {
  const result = element("section", `panel ${className}`.trim());
  if (title) {
    const header = element("div", "panel-header");
    append(header, element("h2", "", title));
    append(result, header);
  }
  return result;
}

function badge(value) {
  const result = element("span", "badge", value || "unknown");
  result.dataset.tone = statusTone(value);
  return result;
}

function button(label, className = "secondary", onClick = null) {
  const result = element("button", `button ${className}`, label);
  result.type = "button";
  if (onClick) result.addEventListener("click", onClick);
  return result;
}

function loading(name) {
  return state.loading.has(name);
}

function setLoading(name, active) {
  active ? state.loading.add(name) : state.loading.delete(name);
}

function requestController(name) {
  controllers.get(name)?.abort();
  const controller = new AbortController();
  controllers.set(name, controller);
  return controller;
}

function handleError(error, area, notify = true) {
  if (error?.name === "AbortError") return;
  const message = error instanceof ApiError ? error.message : error?.message || String(error);
  state.errors[area] = message;
  if (notify) toast(message, "danger");
}

function markRefresh() {
  state.lastRefresh = new Date();
  lastUpdated.textContent = `Last signal ${state.lastRefresh.toLocaleTimeString()}`;
}

function toast(message, tone = "neutral", timeout = 5000) {
  const item = element("div", "toast", message);
  item.dataset.tone = tone;
  toastRegion.append(item);
  window.setTimeout(() => item.remove(), timeout);
}

function updateConnection(next) {
  state.connection = next;
  connectionStatus.dataset.state = next;
  connectionLabel.textContent = next === "live" ? "LIVE LINK" : next === "offline" ? "RECONNECTING" : "CONNECTING";
  document.querySelectorAll(".run-pulse").forEach((pulse) => {
    pulse.dataset.connection = next;
  });
}

async function refreshSystem({ quiet = false } = {}) {
  const controller = requestController("system");
  setLoading("system", true);
  try {
    const [health, version, models, init, stats, telemetrySettings] = await Promise.all([
      QuillApi.health(controller.signal),
      QuillApi.version(controller.signal),
      QuillApi.models(controller.signal),
      QuillApi.init(controller.signal),
      QuillApi.stats(controller.signal),
      QuillApi.telemetrySettings(controller.signal),
    ]);
    Object.assign(state, { health, version, models, init, stats, telemetrySettings });
    delete state.errors.system;
    footerVersion.textContent = `QUILL ${version.quill}`;
    markRefresh();
  } catch (error) {
    handleError(error, "system", !quiet);
  } finally {
    setLoading("system", false);
    if (state.route.section === "overview") updateOverviewRegions({ settled: true });
    // Routes that render system data need the paint that follows it. Without this a hard refresh
    // onto one of them shows an empty page until a hashchange happens to re-render it.
    else if (["models", "settings", "api"].includes(state.route.section) && !openSelect) render();
    else if (["settings", "api"].includes(state.route.section)) render();
  }
}

async function refreshRuns({ quiet = false, includeInspector = true } = {}) {
  const controller = requestController("runs");
  setLoading("runs", true);
  const selected = state.route.section === "runs" ? state.route.id : null;
  try {
    const jobs = [
      QuillApi.queue(controller.signal),
      QuillApi.runs({ ...state.runFilters, limit: 200 }, controller.signal),
      QuillApi.runs({ limit: 200 }, controller.signal),
      QuillApi.runs({ limit: 25, offset: state.overviewRunPage.offset }, controller.signal),
    ];
    if (selected && includeInspector) {
      jobs.push(
        QuillApi.run(selected, controller.signal),
        QuillApi.breakdown(selected, controller.signal),
        QuillApi.artifacts(selected, controller.signal),
        QuillApi.restartOptions(selected, controller.signal),
      );
    }
    const [queue, runs, facets, overviewRuns, detail, breakdown, artifacts, restartOptions] = await Promise.all(jobs);
    state.queue = queue;
    state.runs = runs.runs || [];
    state.runPage = {
      limit: Number(runs.limit || 200),
      offset: Number(runs.offset || 0),
      hasMore: Boolean(runs.has_more),
    };
    state.runFacets = facets.runs || [];
    state.overviewRuns = overviewRuns.runs || [];
    state.overviewRunPage = {
      limit: Number(overviewRuns.limit || 25),
      offset: Number(overviewRuns.offset || 0),
      hasMore: Boolean(overviewRuns.has_more),
    };
    const visibleIds = new Set(state.runs.map((run) => run.run_id));
    state.selectedRunIds = new Set(
      [...state.selectedRunIds].filter((runId) => visibleIds.has(runId)),
    );
    if (selected && includeInspector) {
      state.runDetail = detail;
      state.breakdown = breakdown;
      state.artifacts = artifacts.artifacts || [];
      state.restartOptions = restartOptions;
      if (state.artifact?.runId !== selected) state.artifact = null;
    }
    delete state.errors.runs;
    markRefresh();
  } catch (error) {
    handleError(error, "runs", !quiet);
  } finally {
    setLoading("runs", false);
    if (state.route.section === "overview") updateLiveRegions(null, { settled: true });
    else if (state.route.section === "runs") updateLiveRegions(selected, { settled: true });
  }
}

async function refreshGitHubRepositories({ quiet = false } = {}) {
  const controller = requestController("github-repositories");
  setLoading("github-repositories", true);
  try {
    const listing = await QuillApi.githubRepositories(controller.signal);
    state.github.login = listing.login || "";
    state.github.repositories = listing.repositories || [];
    const queueRepositories = queueCapableRepositories(state.github.repositories);
    state.queuePage.repo = chooseSelection(
      queueRepositories.map((repository) => repository.name),
      state.queuePage.repo,
    );
    if (!state.runDraft.repo && state.github.repositories.length) {
      state.runDraft.repo = state.github.repositories[0].name;
    }
    delete state.errors.github;
    if (state.runDraft.repo) {
      await Promise.all([
        refreshGitHubIssues(state.runDraft.repo, { quiet }),
        refreshGitHubWorkflows(state.runDraft.repo, { quiet }),
      ]);
    }
  } catch (error) {
    handleError(error, "github", !quiet);
  } finally {
    setLoading("github-repositories", false);
    if (state.route.section === "runs") render();
  }
}

async function refreshProjectQueue({ quiet = false } = {}) {
  const controller = requestController("project-queue");
  setLoading("project-queue", true);
  try {
    state.projectQueue = await QuillApi.projectQueue(controller.signal);
    delete state.errors["project-queue"];
  } catch (error) {
    handleError(error, "project-queue", !quiet);
  } finally {
    setLoading("project-queue", false);
    if (state.route.section === "queue") updateProjectQueueOrder();
    else if (state.route.section === "overview") updateOverviewQueueRegion();
  }
}

async function refreshQueueCandidates(repo = state.queuePage.repo, { quiet = false } = {}) {
  controllers.get("queue-candidates")?.abort();
  if (!repo) {
    state.queuePage.groups = [];
    state.queuePage.selected.clear();
    if (state.route.section === "queue") render();
    return;
  }
  const controller = requestController("queue-candidates");
  setLoading("queue-candidates", true);
  state.queuePage.repo = repo;
  try {
    const listing = await QuillApi.projectQueueCandidates(repo, controller.signal);
    if (state.queuePage.repo !== repo) return;
    state.queuePage.groups = listing.groups || [];
    state.queuePage.selected = pruneQueueSelection(
      state.queuePage.groups,
      state.queuePage.selected,
    );
    delete state.errors["queue-candidates"];
  } catch (error) {
    if (state.queuePage.repo !== repo) return;
    state.queuePage.groups = [];
    state.queuePage.selected.clear();
    handleError(error, "queue-candidates", !quiet);
  } finally {
    setLoading("queue-candidates", false);
    if (state.route.section === "queue" && !openSelect) updateQueueCandidatesRegion();
  }
}

async function refreshQueuePage({ quiet = false } = {}) {
  if (!state.github.repositories.length) await refreshGitHubRepositories({ quiet });
  await Promise.all([
    refreshProjectQueue({ quiet }),
    refreshQueueCandidates(state.queuePage.repo, { quiet }),
  ]);
}

async function refreshGitHubWorkflows(repo, { quiet = false } = {}) {
  const controller = requestController("github-workflows");
  setLoading("github-workflows", true);
  try {
    const previousWorkflow = state.runDraft.workflow;
    const previousOverride = {
      enabled: state.runDraft.override_models,
      overrides: state.runDraft.model_overrides,
    };
    const listing = await QuillApi.githubWorkflows(repo, controller.signal);
    if (state.runDraft.repo !== repo) return;
    state.github.workflows = listing.workflows || [];
    state.github.models = listing.models || [];
    state.github.excludedIssueLabels = listing.excluded_issue_labels || [];
    applyIssueLabelFilter();
    const selected = state.github.workflows.find((item) => item.id === state.runDraft.workflow)
      || state.github.workflows.find((item) => item.id === listing.default)
      || state.github.workflows[0];
    state.runDraft.workflow = selected?.id || "";
    state.runDraft.mode = selected?.mode || "create";
    const reconciled = reconcileModelOverrides(
      previousOverride,
      selected?.phases || [],
      state.github.models,
      selected?.id === previousWorkflow,
    );
    state.runDraft.override_models = reconciled.enabled;
    state.runDraft.model_overrides = reconciled.overrides;
    await refreshDraftBranch({ quiet: true });
  } catch (error) {
    handleError(error, "github", !quiet);
  } finally {
    setLoading("github-workflows", false);
    if (state.route.section === "runs") render();
  }
}

async function refreshDraftBranch({ quiet = false } = {}) {
  const issue = state.github.issues.find(
    (item) => String(item.number) === String(state.runDraft.ticket),
  );
  if (!issue) {
    state.runDraft.branch = "";
    state.github.updateTarget = null;
    delete state.errors["github-update-target"];
    return;
  }
  if (!new Set(["update", "review"]).has(state.runDraft.mode)) {
    controllers.get("github-update-target")?.abort();
    state.github.updateTarget = null;
    delete state.errors["github-update-target"];
    state.runDraft.branch = branchName(state.runDraft.work_type, issue);
    return;
  }
  const repo = state.runDraft.repo;
  const ticket = state.runDraft.ticket;
  const controller = requestController("github-update-target");
  setLoading("github-update-target", true);
  try {
    const target = await QuillApi.githubUpdateTarget(
      repo,
      ticket,
      state.runDraft.mode === "update",
      controller.signal,
    );
    if (state.runDraft.repo !== repo || state.runDraft.ticket !== ticket) return;
    state.github.updateTarget = target;
    state.runDraft.branch = target.branch || "";
    delete state.errors["github-update-target"];
  } catch (error) {
    if (!new Set(["update", "review"]).has(state.runDraft.mode) || state.runDraft.repo !== repo || state.runDraft.ticket !== ticket) return;
    state.github.updateTarget = null;
    state.runDraft.branch = "";
    handleError(error, "github-update-target", !quiet);
  } finally {
    setLoading("github-update-target", false);
    if (state.route.section === "runs") render();
  }
}

async function refreshGitHubIssues(repo, { quiet = false } = {}) {
  const controller = requestController("github-issues");
  setLoading("github-issues", true);
  state.github.repo = repo;
  try {
    const listing = await QuillApi.githubIssues(repo, controller.signal);
    if (state.github.repo !== repo) return;
    state.github.allIssues = listing.issues || [];
    state.github.allWorkTypes = listing.work_types || [];
    applyIssueLabelFilter();
    const selected = state.github.issues.find(
      (issue) => String(issue.number) === String(state.runDraft.ticket),
    ) || state.github.issues[0] || null;
    state.runDraft.ticket = selected ? String(selected.number) : "";
    await refreshDraftBranch({ quiet: true });
    delete state.errors.github;
  } catch (error) {
    handleError(error, "github", !quiet);
  } finally {
    setLoading("github-issues", false);
    if (state.route.section === "runs") render();
  }
}

function applyIssueLabelFilter() {
  const excluded = new Set(
    (state.github.excludedIssueLabels || []).map((label) => String(label).toLowerCase()),
  );
  state.github.issues = (state.github.allIssues || []).filter(
    (issue) => !(issue.labels || []).some((label) => excluded.has(String(label).toLowerCase())),
  );
  const selected = state.github.issues.find(
    (issue) => String(issue.number) === String(state.runDraft.ticket),
  ) || state.github.issues[0] || null;
  state.runDraft.ticket = selected ? String(selected.number) : "";
  const issueLabels = (selected?.labels || []).filter(
    (label) => !excluded.has(String(label).toLowerCase()),
  );
  state.github.work_types = issueLabels.length
    ? [...new Set(issueLabels)]
    : (state.github.allWorkTypes || []).filter(
      (label) => !excluded.has(String(label).toLowerCase()),
    );
  state.runDraft.work_type = preferredWorkType(selected, state.github.work_types);
}

async function refreshWorkspaces({ quiet = false } = {}) {
  const controller = requestController("workspaces");
  setLoading("workspaces", true);
  try {
    const listing = await QuillApi.workspaces(controller.signal);
    state.workspaces.list = listing.workspaces || [];
    // Keep the operator's selection across refreshes; fall back to the first checkout only when the
    // selected one is gone. Selection is by repo identifier, never a list index.
    state.workspaces.repo = chooseSelection(
      state.workspaces.list.map((item) => item.repo),
      state.workspaces.repo,
    );
    delete state.errors.workspaces;
    markRefresh();
    if (state.workspaces.repo) {
      await refreshWorkspaceBranches(state.workspaces.repo, { quiet });
    } else {
      state.workspaces.branches = [];
      state.workspaces.current = null;
      state.workspaces.branch = "";
    }
  } catch (error) {
    handleError(error, "workspaces", !quiet);
  } finally {
    setLoading("workspaces", false);
    if (state.route.section === "workspaces" && !openSelect) render();
  }
}

async function refreshWorkspaceBranches(repo, { quiet = false } = {}) {
  const controller = requestController("workspace-branches");
  setLoading("workspace-branches", true);
  state.workspaces.repo = repo;
  try {
    const listing = await QuillApi.workspaceBranches(repo, controller.signal);
    if (state.workspaces.repo !== repo) return; // a newer selection superseded this fetch
    state.workspaces.branches = listing.branches || [];
    state.workspaces.current = listing.current ?? null;
    const names = state.workspaces.branches.map((item) => item.name);
    const preferred = names.includes(state.workspaces.branch)
      ? state.workspaces.branch
      : listing.current ?? "";
    state.workspaces.branch = chooseSelection(names, preferred);
    delete state.errors.workspaces;
  } catch (error) {
    state.workspaces.branches = [];
    state.workspaces.current = null;
    state.workspaces.branch = "";
    handleError(error, "workspaces", !quiet);
  } finally {
    setLoading("workspace-branches", false);
    if (state.route.section === "workspaces" && !openSelect) render();
  }
}

async function refreshCatalog(kind, selected = state.route.id, { quiet = false } = {}) {
  const controller = requestController(`catalog-${kind}`);
  setLoading(kind, true);
  try {
    const listing = kind === "personas" ? await QuillApi.personas(controller.signal) : await QuillApi.skills(controller.signal);
    if (kind === "personas") {
      state.personas = listing.entries || [];
      state.personaRoot = listing.root || "";
      if (selected && !state.personaCreating) state.persona = await QuillApi.persona(selected, controller.signal);
    } else {
      state.skills = listing.entries || [];
      state.skillRoot = listing.root || "";
      if (selected && !state.skillCreating) state.skill = await QuillApi.skill(selected, controller.signal);
    }
    delete state.errors[kind];
    markRefresh();
  } catch (error) {
    handleError(error, kind, !quiet);
  } finally {
    setLoading(kind, false);
    if (state.route.section === kind) render();
  }
}

async function refreshMemories({ quiet = false } = {}) {
  const controller = requestController("memories");
  setLoading("memories", true);
  try {
    const listing = await QuillApi.memories(controller.signal);
    state.memories = listing.memories || [];
    state.memoryArchivedEvents = Number(listing.archived_events) || 0;
    const available = new Set(state.memories.map((memory) => memory.memory_id));
    state.selectedMemoryIds = new Set(
      [...state.selectedMemoryIds].filter((memoryId) => available.has(memoryId)),
    );
    const repositories = [...new Set(state.memories.map((memory) => memory.repo))].sort();
    if (state.memoryRepo && !repositories.includes(state.memoryRepo)) state.memoryRepo = "";
    delete state.errors.memories;
    markRefresh();
  } catch (error) {
    handleError(error, "memories", !quiet);
  } finally {
    setLoading("memories", false);
    if (state.route.section === "memories" && !openSelect) render();
  }
}

function connectEvents() {
  eventSource?.close();
  updateConnection("connecting");
  eventSource = new EventSource("/events");
  eventSource.onopen = () => updateConnection("connecting");
  eventSource.onerror = () => updateConnection("offline");
  eventSource.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    // No revision de-dup: every event the backend fires is applied the instant it arrives. Ordering
    // is guaranteed by the single SSE connection; a `sync` re-establishes truth on reconnect.
    if (event.type === "sync") {
      state.queue = event.queue || state.queue;
      state.projectQueue = event.project_queue || state.projectQueue;
      mergeLiveRuns(event.runs || [], true);
      updateConnection("live");
      updateLiveRegions();
      if (state.route.section === "queue") {
        updateProjectQueueOrder();
        refreshQueueCandidates(state.queuePage.repo, { quiet: true });
      }
      return;
    }
    if (event.type === "resync_required") {
      updateConnection("connecting");
      refreshRuns({ quiet: true });
      if (["overview", "queue"].includes(state.route.section)) refreshProjectQueue({ quiet: true });
      if (state.route.section === "queue") refreshQueueCandidates(state.queuePage.repo, { quiet: true });
      return;
    }
    updateConnection("live");
    if (event.type === "queue_updated") state.queue = event.queue || state.queue;
    if (event.type === "project_queue_updated") {
      state.projectQueue = event.project_queue || state.projectQueue;
      if (state.route.section === "queue") {
        updateProjectQueueOrder();
        refreshQueueCandidates(state.queuePage.repo, { quiet: true });
      }
      if (state.route.section === "overview") updateOverviewQueueRegion();
    }
    if (event.run) mergeLiveRuns([event.run]);
    if (event.run && ["done", "failed", "halted"].includes(event.run.status)) {
      QuillApi.stats().then((stats) => {
        state.stats = stats;
        if (state.route.section === "overview") updateOverviewRegions({ settled: true });
      }).catch(() => {});
    }
    // Live per-phase usage (tokens/cost/tools) pushed on every tool call — apply straight from the
    // payload, no refetch. This is the real-time heartbeat during a long, otherwise-silent phase.
    if (event.usage && event.run_id) state.liveUsage[event.run_id] = event.usage;
    // tool_progress fires constantly; keep it out of the discrete event feed and out of the
    // inspector-refetch path (its data is already in the payload).
    const isProgress = ["tool_progress", "usage_progress"].includes(event.type);
    if (!isProgress && !event.type?.includes("queue_")) {
      state.recentEvents.unshift(event);
      state.recentEvents = state.recentEvents.slice(0, 6);
    }
    if (isProgress) {
      updateProgressRegions(event.run_id);
      return;
    }
    triggerEventPulse();
    updateLiveRegions(event.run_id);
    if (!isProgress && event.run && state.route.id === event.run.run_id) {
      window.clearTimeout(detailRefreshTimer);
      detailRefreshTimer = window.setTimeout(() => refreshRunInspector(event.run.run_id), 180);
    }
  };
}

function mergeLiveRuns(runs, replaceLive = false) {
  for (const run of runs || []) {
    if (run.live_usage && Object.keys(run.live_usage).length) state.liveUsage[run.run_id] = run.live_usage;
    if (state.runDetail?.run_id === run.run_id) {
      state.runDetail = { ...state.runDetail, ...run };
    }
    const phaseStartedAt = Number(run.phase_started_at);
    if (run.phase && (
      state.livePhase[run.run_id] !== run.phase
      || (phaseStartedAt && state.phaseStartedAt[run.run_id] !== phaseStartedAt)
    )) {
      state.livePhase[run.run_id] = run.phase;
      state.phaseStartedAt[run.run_id] = phaseStartedAt || Date.now() / 1000;
    }
  }
  const liveIds = new Set((runs || []).map((run) => run.run_id));
  const history = replaceLive
    ? state.runs.filter((run) => !["queued", "running", "needs_decision"].includes(run.status) && !liveIds.has(run.run_id))
    : state.runs;
  const merged = new Map(history.map((run) => [run.run_id, run]));
  for (const run of runs || []) merged.set(run.run_id, run);
  state.runs = [...merged.values()].sort((left, right) => Number(right.queued_at) - Number(left.queued_at));
}

async function refreshRunInspector(runId) {
  if (state.route.section !== "runs" || state.route.id !== runId) return;
  const controller = requestController("run-inspector");
  try {
    const [detail, breakdown, artifacts, restartOptions] = await Promise.all([
      QuillApi.run(runId, controller.signal), QuillApi.breakdown(runId, controller.signal),
      QuillApi.artifacts(runId, controller.signal), QuillApi.restartOptions(runId, controller.signal),
    ]);
    if (state.route.section !== "runs" || state.route.id !== runId) return;
    state.runDetail = detail;
    state.breakdown = breakdown;
    state.artifacts = artifacts.artifacts || [];
    state.restartOptions = restartOptions;
    updateLiveRegions(runId, { settled: true });
  } catch (error) { handleError(error, "runs", false); }
}

function updateLiveRegions(runId = null, { settled = false } = {}) {
  if (state.route.section === "overview") {
    updateRunPulse(activeDisplayRun());
    updateOverviewRegions({ settled });
    return;
  }
  if (state.route.section !== "runs") return;
  if (!state.route.id) {
    updateRunsListing();
    return;
  }
  if (runId && runId !== state.route.id) return;
  const run = state.runDetail || state.runs.find((item) => item.run_id === state.route.id);
  updateRunPulse(run);
  updatePhaseGraphPanel(run);
  if (settled) updateRunInspectorRegion();
  updateElapsed();
}

function replaceMountedRegion(name, nextRoot) {
  const mounted = document.querySelector(`[data-live-region="${name}"]`);
  const next = nextRoot?.querySelector?.(`[data-live-region="${name}"]`)
    || (nextRoot?.dataset?.liveRegion === name ? nextRoot : null);
  if (!mounted || !next || mounted.contains(document.activeElement)) return;
  mounted.replaceWith(next);
}

function updateOverviewRegions({ settled = false } = {}) {
  if (state.route.section !== "overview") return;
  const fresh = renderOverview({ supportingOnly: true });
  for (const name of ["current-phase", "recent-signals", "recent-runs"]) {
    replaceMountedRegion(name, fresh);
  }
  if (settled) replaceMountedRegion("statistics", fresh);
  else updateOverviewQueueRegion();
  updateElapsed();
}

function updateOverviewQueueRegion() {
  if (state.route.section !== "overview") return;
  const mounted = document.querySelector('[data-live-region="overview-queue"]');
  if (!mounted) return;
  const next = renderOverviewQueueCard();
  mounted.replaceChildren(...next.childNodes);
}

function updateRunsListing() {
  if (state.route.section !== "runs" || state.route.id) return;
  replaceMountedRegion("runs-listing", renderRunsListing());
  updateElapsed();
}

function updateProjectQueueOrder() {
  if (state.route.section !== "queue") return;
  const mounted = document.querySelector('[data-live-region="project-queue-order"]');
  if (!mounted) return;
  const next = renderProjectQueueOrder();
  mounted.replaceChildren(...next.childNodes);
}

async function connectTelemetry() {
  telemetrySource?.close();
  try {
    state.telemetry = await QuillApi.telemetry();
    updateTelemetryGauges();
  } catch (error) { handleError(error, "telemetry", false); }
  telemetrySource = new EventSource("/telemetry/events");
  telemetrySource.addEventListener("telemetry", (message) => {
    try { state.telemetry = JSON.parse(message.data); } catch { return; }
    updateTelemetryGauges();
    updateModelSwitchRegions();
  });
}

function triggerEventPulse() {
  document.querySelectorAll(".run-pulse").forEach((pulse) => {
    pulse.classList.remove("event-hit");
    void pulse.offsetWidth;
    pulse.classList.add("event-hit");
    window.setTimeout(() => pulse.classList.remove("event-hit"), 950);
  });
}

function activeDisplayRun() {
  if (state.route.section === "runs" && state.runDetail) return state.runDetail;
  return state.queue.active || state.queue.queued?.[0] || null;
}

function runFailureMessage(run) {
  if (!run?.error) return "";
  return run.failure_label ? `${run.failure_label}\n${run.error}` : run.error;
}

function renderRunPulse(run = activeDisplayRun()) {
  const layout = element("div", "run-pulse-stack");
  layout.dataset.liveRegion = "run-pulse-stack";
  const result = panel(null, "run-pulse");
  result.dataset.runPulse = "true";

  const visual = element("div", "pulse-visual");
  const core = element("div", "pulse-core");
  core.dataset.runPulseCore = "true";
  append(
    visual,
    element("div", "pulse-ring"),
    element("div", "pulse-ring ring-two"),
    element("div", "pulse-orbit"),
    element("div", "pulse-ripple"),
    core,
  );

  const copy = element("div", "pulse-copy");
  const eyebrow = element("p", "eyebrow");
  eyebrow.dataset.runPulseEyebrow = "true";
  const title = element("h2");
  title.dataset.runPulseTitle = "true";
  append(copy, eyebrow, title);
  const live = element("div");
  live.dataset.runPulseStatus = "true";
  live.setAttribute("aria-live", "polite");
  copy.append(live);
  const question = element("div");
  question.dataset.runPulseQuestion = "true";
  const error = element("div");
  error.dataset.runPulseError = "true";
  const meta = element("div", "pulse-meta");
  meta.dataset.runPulseMeta = "true";
  append(copy, question, error, meta);
  append(result, visual, copy);
  const resources = panel("System Telemetry", "resource-panel");
  resources.dataset.systemTelemetry = "true";
  resources.querySelector(".panel-header")?.append(renderVllmThroughput());
  resources.append(renderTelemetryGauges());
  append(layout, result, resources);
  applyRunPulse(result, run);
  return layout;
}

function renderOverviewRunPulse(run = activeDisplayRun()) {
  const layout = element("div", "overview-pulse-stack");
  const pulseStack = renderRunPulse(run);
  const pulse = pulseStack.querySelector(".run-pulse");
  const telemetry = pulseStack.querySelector(".resource-panel");
  const current = renderCurrentPhase(run);
  const row = element("div", "overview-run-row");
  append(row, pulse, current);
  append(layout, row, telemetry);
  return layout;
}

function activePhaseRows(run) {
  if (!run) return [];
  const nodes = new Map((run.phase_graph?.nodes || []).map((node) => [node.id, node]));
  const usage = state.liveUsage[run.run_id] || run.live_usage || {};
  const phases = Object.entries(run.active_phases || {});
  if (phases.length) {
    return phases.map(([phase, startedAt]) => {
      const phaseUsage = usage.active_phase_usages?.[phase]
        || usage.phase_usages?.[phase]
        || (phase === run.phase ? usage.phase_usage : {})
        || {};
      const modelLoad = [...(run.model_loads || [])].reverse().find((load) => load.phase === phase);
      const currentModel = phase === run.phase ? run.model : null;
      return {
        phase,
        label: nodes.get(phase)?.label || (phase === run.phase ? run.phase_label : null) || phase,
        model: currentModel || modelLoad?.model || null,
        startedAt,
        tokens: phaseUsage.context_window_tokens ?? phaseUsage.total_tokens ?? 0,
        tools: phaseUsage.tool_calls_total ?? 0,
      };
    });
  }
  return [];
}

function renderCurrentPhase(run = activeDisplayRun()) {
  const result = panel("Current Phase", "current-phase-panel");
  result.dataset.liveRegion = "current-phase";
  if (!run) {
    append(result, element("div", "current-phase-empty", "No run is executing."));
    return result;
  }
  result.classList.add("current-phase-link");
  result.tabIndex = 0;
  result.setAttribute("role", "link");
  result.setAttribute("aria-label", `Open Breakdown for run ${run.run_id}`);
  const open = () => {
    state.runTab = "breakdown";
    location.hash = `#/runs/${encodeURIComponent(run.run_id)}`;
  };
  result.addEventListener("click", open);
  result.addEventListener("keydown", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    open();
  });

  const rows = activePhaseRows(run);
  if (!rows.length) {
    const activity = element("div", "current-phase-activity");
    append(
      activity,
      badge(run.status),
      element("strong", "", run.activity_label || liveRunLabel(run)),
      element("span", "mono muted", run.activity === "loading_model" ? "MODEL LOAD" : `RUN ${run.run_id}`),
    );
    append(result, activity);
    return result;
  }
  const list = element("div", "current-phase-list");
  for (const item of rows) {
    const row = element("div", "current-phase-row");
    row.dataset.state = "active";
    row.dataset.livePhaseRunId = run.run_id;
    row.dataset.livePhaseId = item.phase;
    row.dataset.livePhasePriorTokens = "0";
    const identity = element("div", "current-phase-identity");
    append(
      identity,
      element("strong", "", item.label),
      element("span", "current-phase-model mono", item.model ? `MODEL · ${item.model}` : "MODEL · NOT REPORTED"),
    );
    const metrics = element("div", "current-phase-metrics");
    const duration = element("span", "current-phase-metric current-phase-duration mono", formatDuration(0));
    duration.dataset.livePhaseStarted = String(item.startedAt);
    const tokens = element("span", "current-phase-metric current-phase-tokens mono", `${formatNumber(item.tokens)} tokens`);
    tokens.dataset.livePhaseTokens = "true";
    tokens.dataset.tokenSuffix = " tokens";
    const tools = element("span", "current-phase-metric current-phase-tools mono", `${formatNumber(item.tools)} tools`);
    tools.dataset.livePhaseToolsSummary = "true";
    tools.dataset.toolSuffix = " tools";
    append(metrics, badge("active"), duration, tokens, tools);
    append(row, identity, metrics);
    list.append(row);
  }
  result.append(list);
  return result;
}

function updateRunPulse(run = activeDisplayRun()) {
  const result = document.querySelector("[data-run-pulse]");
  if (!result) return;
  applyRunPulse(result, run);
}

function applyRunPulse(result, run) {
  const status = run?.status || "idle";
  const terminal = ["done", "failed", "halted"].includes(status);
  result.dataset.status = status;
  result.dataset.activity = run?.activity || status;
  result.dataset.terminal = String(terminal);
  result.dataset.connection = state.connection;
  if (run) result.dataset.liveRunId = run.run_id;
  else delete result.dataset.liveRunId;
  result.querySelector("[data-run-pulse-core]").textContent = status === "idle"
    ? "Q"
    : String(run.ticket || "Q");
  result.querySelector("[data-run-pulse-eyebrow]").textContent = status === "idle"
    ? "SYSTEM IDLE"
    : `RUN ${run.run_id}`;
  result.querySelector("[data-run-pulse-title]").textContent = liveRunLabel(run);
  result.querySelector("[data-run-pulse-status]").replaceChildren(badge(status));
  const question = result.querySelector("[data-run-pulse-question]");
  question.replaceChildren(...(run?.question ? [element("p", "notice warning", run.question)] : []));
  const error = result.querySelector("[data-run-pulse-error]");
  error.replaceChildren(...(run?.error ? [diagnostic(runFailureMessage(run), "notice danger")] : []));
  const meta = result.querySelector("[data-run-pulse-meta]");
  if (!run) {
    meta.replaceChildren(element("span", "", "No active or queued work"));
    return;
  }
  meta.replaceChildren();
  append(
    meta,
    element("span", "", `${run.repo || "unknown repo"} #${run.ticket ?? "—"}`),
    element("span", "", `${run.workflow || "legacy ticket"} workflow${run.pr_number ? ` · PR #${run.pr_number}` : ""}`),
    run.phase_label || run.phase ? element("span", "", `phase ${run.phase_label || run.phase}`) : null,
    elapsedNode(run.started_at, terminal ? run.updated_at : null),
    run.queue_position !== null && run.queue_position !== undefined
      ? element("span", "", `queue position ${run.queue_position}`)
      : null,
  );
}

function renderVllmThroughput() {
  const result = element("div", "vllm-throughput");
  result.dataset.vllmThroughput = "true";
  result.setAttribute("aria-label", "vLLM rolling token throughput");
  append(
    result,
    throughputMetric("model", "MODEL"),
    throughputMetric("processing", "PROCESSING"),
    throughputMetric("generation", "GENERATION"),
  );
  applyVllmThroughput(result, state.telemetry?.vllm || {});
  return result;
}

function throughputMetric(kind, label) {
  const metric = element("div", "vllm-throughput-metric");
  metric.dataset.throughput = kind;
  append(metric, element("span", "vllm-throughput-label", label), element("strong", "vllm-throughput-value", "— tok/s"));
  return metric;
}

function updateVllmThroughput() {
  const region = document.querySelector("[data-vllm-throughput]");
  if (!region) return;
  applyVllmThroughput(region, state.telemetry?.vllm || {});
}

function applyVllmThroughput(region, throughput) {
  const modelMetric = region.querySelector('[data-throughput="model"]');
  const loadedModels = Array.isArray(throughput.loaded_models)
    ? throughput.loaded_models.filter((model) => typeof model === "string" && model)
    : [];
  modelMetric.querySelector(".vllm-throughput-value").textContent = loadedModels.join(", ") || "—";
  modelMetric.title = loadedModels.length
    ? `Loaded vLLM model${loadedModels.length === 1 ? "" : "s"}: ${loadedModels.join(", ")}`
    : "No model advertised by vLLM metrics";
  for (const [kind, field, samples] of [
    ["processing", "processing_tokens_per_second", "processing_samples"],
    ["generation", "generation_tokens_per_second", "generation_samples"],
  ]) {
    const metric = region.querySelector(`[data-throughput="${kind}"]`);
    const value = Number(throughput[field]);
    const count = Math.max(0, Number(throughput[samples]) || 0);
    metric.querySelector(".vllm-throughput-value").textContent = Number.isFinite(value)
      ? `${value.toFixed(1)} tok/s`
      : "— tok/s";
    metric.title = count ? `Running average across ${count} active samples` : "Waiting for active vLLM samples";
  }
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

function renderPhaseGraphPanel(run = activeDisplayRun()) {
  const result = panel(null, "phase-graph-panel");
  result.dataset.liveRegion = "phase-graph";
  const header = element("div", "phase-graph-header");
  const title = element("div");
  append(title, element("p", "eyebrow", "WORKFLOW ROUTES"), element("h2", "", "Phase graph"));
  append(header, title);
  result.append(header);

  const region = element("div", "phase-graph-region");
  region.dataset.phaseGraphRegion = "true";
  if (!run) {
    append(region, element("div", "phase-graph-empty", "Phase graph available when execution starts."));
  } else {
    const executions = state.breakdown?.run_id === run.run_id
      ? state.breakdown.phase_executions || []
      : [];
    const liveUsage = state.liveUsage[run.run_id] || run.live_usage || {};
    const graphMetrics = phaseGraphMetrics(executions, liveUsage);
    run = {
      ...run,
      phase_execution_counts: graphMetrics.executionCounts,
      phase_token_counts: graphMetrics.tokenCounts,
    };
    const graph = normalizePhaseGraph(run);
    if (!graph?.nodes.length) {
      const message = run.status === "queued"
        ? "Phase graph available when execution starts."
        : "No structured phase graph is available for this run.";
      append(region, element("div", "phase-graph-empty", message));
    } else {
      const layout = layoutPhaseGraph(graph);
      const states = phaseNodeStates(run, graph);
      const wrap = element("div", "phase-graph-scroll");
      wrap.dataset.phaseGraphRunId = run.run_id;
      wrap.dataset.phaseGraphStructure = phaseGraphStructureSignature(graph);
      const svg = svgElement("svg", { viewBox: `0 0 ${layout.width} ${layout.height}`, width: layout.width, height: layout.height, role: "img", "aria-label": "Configured phase routes and traversal counts" });
      const defs = svgElement("defs");
      for (const markerState of ["unvisited", "completed", "active", "failed"]) {
        const arrow = svgElement("marker", {
          id: `phase-flow-arrow-${markerState}`,
          class: "phase-arrow",
          "data-state": markerState,
          viewBox: "-2 -2 12 12",
          refX: 7,
          refY: 4,
          markerWidth: 6,
          markerHeight: 6,
          orient: "auto-start-reverse",
          markerUnits: "strokeWidth",
        });
        append(
          arrow,
          svgElement("path", { class: "phase-arrow-underlay", d: "M -1 -1 L 9 4 L -1 9 Z" }),
          svgElement("path", { class: "phase-arrow-head", d: "M 0 0 L 8 4 L 0 8 Z" }),
        );
        append(defs, arrow);
      }
      svg.append(defs);
      // Containers first so lanes and routes draw on top of them.
      for (const group of layout.groups || []) {
        const containerState = groupState(group, layout.nodes, states);
        const container = svgElement("g", {
          class: "phase-group",
          "data-state": containerState,
          "data-phase-group-id": group.id,
        });
        const box = svgElement("rect", {
          x: group.x,
          y: group.y,
          width: group.width,
          height: group.height,
          rx: 12,
        });
        const caption = svgElement("text", { x: group.labelX, y: group.labelY });
        caption.textContent = group.label;
        const groupTitle = svgElement("title");
        groupTitle.textContent =
          `${group.label}: ${group.memberCount} concurrent phases · ${containerState}`;
        append(container, groupTitle, box, caption);
        svg.append(container);
      }
      for (const edge of layout.edges) {
        const edgeState = phaseEdgeState(edge, states);
        const group = svgElement("g", {
          class: "phase-edge",
          "data-state": edgeState,
          "data-phase-edge-key": edge.key,
        });
        const path = svgElement("path", {
          d: edge.path,
          ...(edge.showArrow ? { "marker-end": `url(#phase-flow-arrow-${edgeState})` } : {}),
        });
        const labelAt = edgeLabelPosition(edge, layout);
        const label = edge.lane > 0 && edge.count > 0
          ? svgElement("text", { x: labelAt.x, y: labelAt.y, "text-anchor": "middle" })
          : null;
        if (label) label.textContent = String(edge.count);
        const titleNode = svgElement("title");
        const contractText = contractEdgeLabel(edge.contracts);
        titleNode.textContent = `${edge.source} to ${edge.target}, traversed ${edge.count} times${contractText ? ` · contracts: ${contractText}` : ""}`;
        const contractLabel = contractText
          ? svgElement("text", { x: labelAt.x, y: labelAt.y + 10, "text-anchor": "middle", class: "phase-edge-contract" })
          : null;
        if (contractLabel) contractLabel.textContent = contractText;
        append(group, titleNode, path, label, contractLabel);
        svg.append(group);
      }
      for (const node of layout.nodes) {
        const group = svgElement("g", {
          transform: `translate(${node.x} ${node.y})`,
          "data-phase-node-id": node.id,
        });
        const main = svgElement("g", {
          class: "phase-node",
          "data-state": states[node.id],
          "data-kind": node.type,
        });
        const mainWidth = node.width;
        const rect = svgElement("rect", { width: mainWidth, height: node.height, rx: 9 });
        const liveStarted = states[node.id] !== "active"
          ? 0
          : node.type === "model_load"
            ? Number(node.loadStartedAt)
            : Number(run.active_phases?.[node.id])
              || (["running", "needs_decision"].includes(run.status) && node.id === run.phase
                ? Number(run.phase_started_at || state.phaseStartedAt[run.run_id])
                : 0);
        const isLive = liveStarted > 0;
        const rows = nodeTextRows(node.height);
        const duration = node.durationSeconds === null && !isLive ? null : svgElement("text", {
          x: mainWidth / 2,
          y: rows.topY,
          class: "phase-node-duration",
          "text-anchor": "middle",
        });
        if (duration && isLive) {
          duration.dataset.livePhaseStarted = String(liveStarted || Date.now() / 1000);
          duration.dataset.livePhaseBase = String(node.durationSeconds || 0);
        } else if (duration) {
          duration.textContent = formatPhaseDuration(node.durationSeconds);
        }
        const phaseId = svgElement("text", {
          x: mainWidth / 2,
          y: 28,
          class: "phase-node-id",
          "text-anchor": "middle",
          "dominant-baseline": "middle",
        });
        phaseId.textContent = node.displayId;
        const executionCount = svgElement("text", {
          x: rows.gutterX,
          y: rows.topY,
          class: "phase-node-count",
        });
        executionCount.textContent = `x${node.executionCount}`;
        const tokens = svgElement("text", {
          x: mainWidth / 2,
          y: rows.bottomY,
          class: "phase-node-tokens",
          "text-anchor": "middle",
        });
        tokens.textContent = node.type === "model_load"
          ? node.modelName
          : `${formatNumber(node.totalTokens)} tokens`;
        if (isLive && node.type !== "model_load") {
          tokens.dataset.livePhaseGraphRunId = run.run_id;
          tokens.dataset.livePhaseGraphId = node.id;
        }
        const titleNode = svgElement("title");
        const contract = node.contractState;
        titleNode.textContent = `${node.label}: ${states[node.id]}${contract ? ` · contract attempt ${contract.attempt}: ${contract.state}${contract.status ? ` (${contract.status})` : ""}${contract.kind ? ` · ${contract.kind}` : ""}` : ""}${node.reason ? ` · ${node.reason}` : ""}`;
        const contractBadge = contract
          ? svgElement("text", {
              x: mainWidth - rows.gutterX,
              y: rows.topY,
              class: "phase-node-contract",
              "data-contract-state": contract.state,
              "text-anchor": "end",
            })
          : null;
        if (contractBadge) contractBadge.textContent = `C${contract.attempt}`;
        append(
          main,
          titleNode,
          rect,
          ...(duration ? [duration] : []),
          phaseId,
          executionCount,
          contractBadge,
          tokens,
        );
        group.append(main);
        if (node.selfCheck) {
          const checkStatus = run.self_checks?.[node.id];
          const checkState = states[node.id];
          const check = svgElement("g", {
            class: "phase-self-check-loop",
            "data-state": checkState,
          });
          const checkTitle = svgElement("title");
          checkTitle.textContent = `Self-check: ${checkStatus || "not run"}`;
          const geometry = selfLoopLayout("SELF CHECK", mainWidth, node.height);
          const loop = svgElement("path", {
            d: geometry.path,
            "marker-end": `url(#phase-flow-arrow-${checkState})`,
          });
          const badge = svgElement("rect", { ...geometry.badge, rx: geometry.badge.height / 2 });
          const badgeText = svgElement("text", {
            x: geometry.captionX,
            y: geometry.captionY,
            "text-anchor": "middle",
            "dominant-baseline": "middle",
          });
          badgeText.textContent = "SELF CHECK";
          append(check, checkTitle, loop, badge, badgeText);
          group.append(check);
        }
        if (node.selfFixRan) {
          const fixStatus = node.selfFixStatus;
          const fixState = fixStatus;
          const fix = svgElement("g", {
            class: "phase-self-fix-loop",
            "data-state": fixState,
          });
          const fixTitle = svgElement("title");
          fixTitle.textContent = `Self-fix: ${fixStatus}`;
          const geometry = selfLoopLayout("SELF FIX", mainWidth, node.height, { below: true });
          const loop = svgElement("path", {
            d: geometry.path,
            "marker-end": `url(#phase-flow-arrow-${fixState})`,
          });
          const badge = svgElement("rect", { ...geometry.badge, rx: geometry.badge.height / 2 });
          const badgeText = svgElement("text", {
            x: geometry.captionX,
            y: geometry.captionY,
            "text-anchor": "middle",
            "dominant-baseline": "middle",
          });
          badgeText.textContent = "SELF FIX";
          append(fix, fixTitle, loop, badge, badgeText);
          group.append(fix);
        }
        svg.append(group);
      }
      const summary = element("ol", "visually-hidden");
      for (const node of layout.nodes) append(summary, element("li", "", `${node.label}: ${states[node.id]}`));
      for (const edge of layout.edges) append(summary, element("li", "", `${edge.source} to ${edge.target}: ${edge.count} traversals`));
      append(wrap, svg, summary);
      region.append(wrap);
      const activeNodes = layout.nodes.filter((node) => states[node.id] === "active");
      const focusKey = activeNodes.map((node) => node.id).sort().join("|");
      const focusCenter = activeNodes.length
        ? (
            Math.min(...activeNodes.map((node) => node.x))
            + Math.max(...activeNodes.map((node) => node.x + node.width))
          ) / 2
        : null;
      wrap.dataset.phaseGraphFocus = focusKey;
      if (focusCenter !== null) wrap.dataset.phaseGraphFocusCenter = String(focusCenter);
      wrap.addEventListener("scroll", () => {
        state.phaseGraphScroll[run.run_id] = wrap.scrollLeft;
      }, { passive: true });
      requestAnimationFrame(() => {
        if (!wrap.isConnected) return;
        const priorFocus = state.phaseGraphFocus[run.run_id] || "";
        const focusChanged = Boolean(focusKey && focusKey !== priorFocus);
        const desired = focusChanged
          ? centeredPhaseScroll(focusCenter, wrap.clientWidth, wrap.scrollWidth)
          : centeredPhaseScroll(
              Number(state.phaseGraphScroll[run.run_id]) + wrap.clientWidth / 2,
              wrap.clientWidth,
              wrap.scrollWidth,
            );
        wrap.scrollTo({ left: desired, behavior: priorFocus && focusChanged ? "smooth" : "auto" });
        state.phaseGraphFocus[run.run_id] = focusKey;
        state.phaseGraphScroll[run.run_id] = desired;
      });
    }
  }
  result.append(region);
  return result;
}

function copyTextAndLiveBindings(current, next) {
  if (!current || !next) return;
  current.textContent = next.textContent;
  for (const name of ["livePhaseStarted", "livePhaseBase", "livePhaseGraphRunId", "livePhaseGraphId"]) {
    if (next.dataset[name] === undefined) delete current.dataset[name];
    else current.dataset[name] = next.dataset[name];
  }
}

function patchPhaseGraphSvg(current, next) {
  for (const nextGroup of next.querySelectorAll("[data-phase-group-id]")) {
    const currentGroup = current.querySelector(`[data-phase-group-id="${CSS.escape(nextGroup.dataset.phaseGroupId)}"]`);
    if (!currentGroup) continue;
    currentGroup.dataset.state = nextGroup.dataset.state;
    currentGroup.querySelector("title").textContent = nextGroup.querySelector("title").textContent;
  }
  for (const nextEdge of next.querySelectorAll("[data-phase-edge-key]")) {
    const currentEdge = current.querySelector(`[data-phase-edge-key="${CSS.escape(nextEdge.dataset.phaseEdgeKey)}"]`);
    if (!currentEdge) continue;
    currentEdge.dataset.state = nextEdge.dataset.state;
    const currentPath = currentEdge.querySelector("path");
    const nextPath = nextEdge.querySelector("path");
    if (nextPath.hasAttribute("marker-end")) currentPath.setAttribute("marker-end", nextPath.getAttribute("marker-end"));
    else currentPath.removeAttribute("marker-end");
    currentEdge.querySelector("title").textContent = nextEdge.querySelector("title").textContent;
    copyTextAndLiveBindings(currentEdge.querySelector("text"), nextEdge.querySelector("text"));
  }
  for (const nextGroup of next.querySelectorAll("[data-phase-node-id]")) {
    const currentGroup = current.querySelector(`[data-phase-node-id="${CSS.escape(nextGroup.dataset.phaseNodeId)}"]`);
    if (!currentGroup) continue;
    const currentNode = currentGroup.querySelector(":scope > .phase-node");
    const nextNode = nextGroup.querySelector(":scope > .phase-node");
    currentNode.dataset.state = nextNode.dataset.state;
    currentNode.querySelector("title").textContent = nextNode.querySelector("title").textContent;
    const currentDuration = currentNode.querySelector(".phase-node-duration");
    const nextDuration = nextNode.querySelector(".phase-node-duration");
    if (!currentDuration && nextDuration) {
      currentNode.insertBefore(nextDuration.cloneNode(true), currentNode.querySelector(".phase-node-id"));
    } else if (currentDuration && !nextDuration) {
      currentDuration.remove();
    } else {
      copyTextAndLiveBindings(currentDuration, nextDuration);
    }
    copyTextAndLiveBindings(currentNode.querySelector(".phase-node-count"), nextNode.querySelector(".phase-node-count"));
    copyTextAndLiveBindings(currentNode.querySelector(".phase-node-tokens"), nextNode.querySelector(".phase-node-tokens"));
    for (const selector of [".phase-self-check-loop", ".phase-self-fix-loop"]) {
      const currentLoop = currentGroup.querySelector(`:scope > ${selector}`);
      const nextLoop = nextGroup.querySelector(`:scope > ${selector}`);
      if (!currentLoop || !nextLoop) continue;
      currentLoop.dataset.state = nextLoop.dataset.state;
      currentLoop.querySelector("title").textContent = nextLoop.querySelector("title").textContent;
      currentLoop.querySelector("path").setAttribute("marker-end", nextLoop.querySelector("path").getAttribute("marker-end"));
    }
  }
}

function updatePhaseGraphPanel(run = activeDisplayRun()) {
  const mounted = document.querySelector('[data-live-region="phase-graph"]');
  if (!mounted) return;
  const currentRegion = mounted.querySelector("[data-phase-graph-region]");
  const nextPanel = renderPhaseGraphPanel(run);
  const nextRegion = nextPanel.querySelector("[data-phase-graph-region]");
  const currentWrap = currentRegion.querySelector(".phase-graph-scroll");
  const nextWrap = nextRegion.querySelector(".phase-graph-scroll");
  if (!currentWrap || !nextWrap || currentWrap.dataset.phaseGraphRunId !== nextWrap.dataset.phaseGraphRunId) {
    currentRegion.replaceChildren(...nextRegion.childNodes);
    return;
  }

  const priorScroll = currentWrap.scrollLeft;
  const structureChanged = currentWrap.dataset.phaseGraphStructure !== nextWrap.dataset.phaseGraphStructure;
  if (structureChanged) {
    currentWrap.replaceChildren(...nextWrap.childNodes);
  } else {
    patchPhaseGraphSvg(currentWrap.querySelector("svg"), nextWrap.querySelector("svg"));
    currentWrap.querySelector(".visually-hidden")?.replaceWith(nextWrap.querySelector(".visually-hidden"));
  }
  currentWrap.dataset.phaseGraphStructure = nextWrap.dataset.phaseGraphStructure;
  currentWrap.dataset.phaseGraphFocus = nextWrap.dataset.phaseGraphFocus;
  if (nextWrap.dataset.phaseGraphFocusCenter === undefined) delete currentWrap.dataset.phaseGraphFocusCenter;
  else currentWrap.dataset.phaseGraphFocusCenter = nextWrap.dataset.phaseGraphFocusCenter;
  currentWrap.scrollLeft = Math.min(priorScroll, Math.max(0, currentWrap.scrollWidth - currentWrap.clientWidth));

  const runId = currentWrap.dataset.phaseGraphRunId;
  const focusKey = currentWrap.dataset.phaseGraphFocus || "";
  const priorFocus = state.phaseGraphFocus[runId] || "";
  if (focusKey && focusKey !== priorFocus) {
    requestAnimationFrame(() => {
      if (!currentWrap.isConnected) return;
      const desired = centeredPhaseScroll(
        Number(currentWrap.dataset.phaseGraphFocusCenter),
        currentWrap.clientWidth,
        currentWrap.scrollWidth,
      );
      currentWrap.scrollTo({ left: desired, behavior: priorFocus ? "smooth" : "auto" });
      state.phaseGraphScroll[runId] = desired;
    });
  } else {
    state.phaseGraphScroll[runId] = currentWrap.scrollLeft;
  }
  state.phaseGraphFocus[runId] = focusKey;
}

function renderTelemetryGauges() {
  const region = element("div", "resource-gauges");
  region.dataset.telemetryGauges = "true";
  region.setAttribute("aria-label", "Live host resource usage");
  const cpu = { key: "cpu", ...state.telemetry?.cpu };
  const cpuGauge = resourceGauge(cpu.key, hardwareLabel(cpu), hardwareVendor(cpu.name, cpu.key));
  applyResourceGauge(cpuGauge, cpu);
  append(region, cpuGauge);
  for (const gpu of state.telemetry?.gpus || []) {
    const reading = { key: `gpu-${gpu.index}`, ...gpu };
    const gauge = resourceGauge(reading.key, hardwareLabel(reading), hardwareVendor(reading.name, reading.key));
    applyResourceGauge(gauge, reading);
    append(region, gauge);
  }
  if (state.telemetry && !(state.telemetry.gpus || []).length) append(region, element("span", "telemetry-empty", "No NVIDIA GPU telemetry"));
  return region;
}

function hardwareLabel(reading) {
  if (reading.key === "cpu") return String(reading.name || "CPU");
  const index = Number.isFinite(Number(reading.index)) ? Number(reading.index) + 1 : "";
  const name = String(reading.name || "GPU").replace(/^NVIDIA\s+(?:GeForce\s+)?/i, "");
  return `${name}${index === "" ? "" : ` ${index}`}`;
}

function hardwareVendor(name, key = "") {
  const value = String(name || "").toLowerCase();
  if (/\b(?:amd|advanced micro devices|ryzen|threadripper|epyc)\b/.test(value)) return "AMD";
  if (/\b(?:nvidia|geforce|quadro|tesla)\b/.test(value)) return "NVIDIA";
  if (/\bintel\b/.test(value) || (String(key).startsWith("gpu") && /\barc\b/.test(value))) return "INTEL";
  if (String(key).startsWith("gpu") && /\bradeon\b/.test(value)) return "AMD";
  return "";
}

function setHardwareVendor(node, vendor) {
  if (node.querySelector("img")?.dataset.vendor === String(vendor || "").toLowerCase()) return;
  node.replaceChildren();
  if (!vendor) return;
  const badges = {
    AMD: "https://img.favpng.com/18/24/4/ryzen-logo-brand-advanced-micro-devices-desktop-wallpaper-png-favpng-iyUQ2UgXECkvqpReUiUCzLW9w.jpg",
    NVIDIA: "https://www.citypng.com/public/uploads/preview/nvidia-geforce-rtx-white-logo-icon-701751694965861o3nds3kinv.png",
    INTEL: "https://toppng.com/uploads/preview/intel-logo-clear-background-11660082424ddoz5sboat.png",
  };
  if (!badges[vendor]) return;
  const badge = element("img", "hardware-vendor");
  badge.dataset.vendor = vendor.toLowerCase();
  badge.src = badges[vendor];
  badge.alt = `${vendor} hardware badge`;
  badge.decoding = "async";
  node.append(badge);
}

function hardwareLabelLines(label) {
  const value = String(label || "");
  const preferred = value.match(/^(.*?\bThreadripper\b)\s+(.+)$/i);
  if (preferred) return [preferred[1], preferred[2]];
  if (value.length <= 30) return [value];
  const spaces = [...value.matchAll(/\s+/g)].map((match) => match.index);
  if (!spaces.length) return [value];
  const split = spaces.reduce((best, index) => (
    Math.abs(index - value.length / 2) < Math.abs(best - value.length / 2) ? index : best
  ), spaces[0]);
  return [value.slice(0, split), value.slice(split).trim()];
}

function setHardwareLabel(node, label) {
  const lines = hardwareLabelLines(label);
  if ([...node.children].map((child) => child.textContent).join("\n") === lines.join("\n")) return;
  node.replaceChildren(...lines.map((line) => element("span", "", line)));
}

function gaugeMetric(kind, label) {
  const metricRow = element("div", `gauge-metric gauge-${kind}`);
  metricRow.dataset.metric = kind;
  const well = element("div", "gauge-horizontal-well");
  well.setAttribute("role", "img");
  append(well, element("div", "gauge-horizontal-fill"), element("strong", "gauge-value", "N/A"));
  append(metricRow, element("span", "gauge-metric-label", label), well);
  return metricRow;
}

function resourceGauge(key, label, vendor = "") {
  const gauge = element("div", "resource-gauge");
  gauge.dataset.gauge = key;
  const bars = element("div", "gauge-bars");
  const memoryKind = key === "cpu" ? "RAM" : "VRAM";
  append(
    bars,
    gaugeMetric("load", "LOAD"),
    gaugeMetric("memory", memoryKind),
    gaugeMetric("temperature", "TEMP"),
    gaugeMetric("fan", "FAN"),
  );
  const labelNode = element("span", "gauge-label");
  const vendorNode = element("span", "gauge-vendor-slot");
  setHardwareVendor(vendorNode, vendor);
  setHardwareLabel(labelNode, label);
  const header = element("div", "gauge-header");
  append(header, vendorNode, labelNode);
  append(gauge, header, bars);
  return gauge;
}

function updateTelemetryGauges() {
  const region = document.querySelector("[data-telemetry-gauges]");
  if (!region) return;
  const readings = [{ key: "cpu", ...state.telemetry?.cpu }, ...(state.telemetry?.gpus || []).map((gpu) => ({ key: `gpu-${gpu.index}`, ...gpu }))];
  for (const empty of region.querySelectorAll(".telemetry-empty")) empty.remove();
  const expected = new Set(readings.map((reading) => reading.key));
  for (const node of region.querySelectorAll("[data-gauge]")) if (!expected.has(node.dataset.gauge)) node.remove();
  for (const reading of readings) {
    let gauge = region.querySelector(`[data-gauge="${reading.key}"]`);
    if (!gauge) {
      gauge = resourceGauge(reading.key, hardwareLabel(reading), hardwareVendor(reading.name, reading.key));
      region.append(gauge);
    }
    applyResourceGauge(gauge, reading);
  }
  if (state.telemetry && !(state.telemetry.gpus || []).length) append(region, element("span", "telemetry-empty", "No NVIDIA GPU telemetry"));
  updateVllmThroughput();
}

function applyResourceGauge(gauge, reading) {
  const isCpu = reading.key === "cpu";
  setHardwareVendor(gauge.querySelector(".gauge-vendor-slot"), hardwareVendor(reading.name, reading.key));
  setHardwareLabel(gauge.querySelector(".gauge-label"), hardwareLabel(reading));
  const load = Number(reading.utilization_percent);
  gauge.style.setProperty("--load", Number.isFinite(load) ? String(Math.max(0, Math.min(100, load))) : "0");
  const loadWell = gauge.querySelector(".gauge-load .gauge-horizontal-well");
  const loadText = formatPercent(reading.utilization_percent);
  gauge.querySelector(".gauge-load .gauge-value").textContent = loadText;
  loadWell.setAttribute("aria-label", `${reading.key === "cpu" ? "CPU" : "GPU"} utilization ${loadText}`);
  const memoryUsed = Number(reading.memory_used_mb);
  const memoryTotal = Number(reading.memory_total_mb);
  const memoryPercent = Number.isFinite(memoryUsed) && Number.isFinite(memoryTotal) && memoryTotal > 0
    ? Math.max(0, Math.min(100, (memoryUsed / memoryTotal) * 100))
    : null;
  gauge.style.setProperty("--memory-load", memoryPercent === null ? "0" : String(memoryPercent));
  const memoryHue = memoryPercent === null ? 120 : 120 * (1 - memoryPercent / 100);
  gauge.style.setProperty("--memory-color", `hsl(${memoryHue} 95% 60%)`);
  const memoryKind = reading.key === "cpu" ? "RAM" : "VRAM";
  const memoryWell = gauge.querySelector(".gauge-memory .gauge-horizontal-well");
  const memoryDescription = memoryPercent === null
    ? `${memoryKind} unavailable`
    : `${memoryKind} ${formatBytes(memoryUsed * 1024 * 1024)} / ${formatBytes(memoryTotal * 1024 * 1024)} (${formatPercent(memoryPercent)})`;
  const memoryCapacity = memoryPercent === null
    ? "—/— GB"
    : `${formatMemoryGb(memoryUsed)}/${formatMemoryGb(memoryTotal)} GB`;
  gauge.querySelector(".gauge-memory .gauge-value").textContent = memoryPercent === null
    ? "N/A"
    : `${formatPercent(memoryPercent)} · ${memoryCapacity}`;
  memoryWell.title = memoryDescription;
  memoryWell.setAttribute("aria-label", memoryDescription);
  const temperature = Number(reading.temperature_c);
  const minimum = Number(state.telemetrySettings?.[isCpu ? "cpu_temperature_min_c" : "gpu_temperature_min_c"]);
  const maximum = Number(state.telemetrySettings?.[isCpu ? "cpu_temperature_max_c" : "gpu_temperature_max_c"]);
  const temperatureLoad = Number.isFinite(temperature) && Number.isFinite(minimum)
    && Number.isFinite(maximum) && maximum > minimum
    ? Math.max(0, Math.min(100, ((temperature - minimum) / (maximum - minimum)) * 100))
    : 0;
  gauge.style.setProperty("--temperature-load", String(temperatureLoad));
  const temperatureHue = 120 * (1 - temperatureLoad / 100);
  gauge.style.setProperty("--temperature-color", `hsl(${temperatureHue} 95% 60%)`);
  const temperatureText = formatTemperature(reading.temperature_c);
  const temperatureWell = gauge.querySelector(".gauge-temperature .gauge-horizontal-well");
  gauge.querySelector(".gauge-temperature .gauge-value").textContent = temperatureText;
  temperatureWell.setAttribute("aria-label", `${isCpu ? "CPU" : "GPU"} temperature ${temperatureText}; scale ${minimum} to ${maximum} degrees Celsius`);
  const fan = Number(reading.fan_percent);
  const fanPercent = Number.isFinite(fan) ? Math.max(0, Math.min(100, fan)) : null;
  gauge.style.setProperty("--fan-load", fanPercent === null ? "0" : String(fanPercent));
  const gpuIndex = Number.isFinite(Number(reading.index)) ? Number(reading.index) : 0;
  const fanText = formatPercent(reading.fan_percent);
  const fanWell = gauge.querySelector(".gauge-fan .gauge-horizontal-well");
  gauge.querySelector(".gauge-fan .gauge-value").textContent = fanText;
  fanWell.setAttribute("aria-label", `${isCpu ? "CPU" : `GPU ${gpuIndex + 1}`} fan speed ${fanText}`);
}

function updateProgressRegions(runId) {
  if (!runId) return;
  const usage = state.liveUsage[runId];
  if (!usage) return;
  document.querySelectorAll("[data-live-usage-run-id]").forEach((node) => {
    if (node.dataset.liveUsageRunId !== runId) return;
    const field = node.dataset.liveUsageField;
    if (field === "execution_tool_calls_total") {
      const completed = Math.max(0, Number(node.dataset.liveCompletedToolCalls) || 0);
      const phases = (node.dataset.liveToolPhases || "").split("\n").filter(Boolean);
      const active = phases.reduce((total, phase) => (
        total + Math.max(0, Number(usage.phase_usages?.[phase]?.tool_calls_total) || 0)
      ), 0);
      node.textContent = formatNumber(completed + active);
    } else {
      node.textContent = field === "cost" ? formatMoney(usage[field]) : formatNumber(usage[field]);
    }
  });
  document.querySelectorAll("[data-live-phase-run-id]").forEach((row) => {
    if (row.dataset.livePhaseRunId !== runId) return;
    const activeUsage = usage.active_phase_usages?.[row.dataset.livePhaseId];
    const phaseUsage = activeUsage || usage.phase_usages?.[row.dataset.livePhaseId] || usage.phase_usage || {};
    const tokens = row.querySelector("[data-live-phase-tokens]");
    const priorTokens = activeUsage
      ? 0
      : Math.max(0, Number(row.dataset.livePhasePriorTokens) || 0);
    if (tokens) {
      const value = formatNumber(
        Math.max(0, (Number(phaseUsage.context_window_tokens ?? phaseUsage.total_tokens) || 0) - priorTokens),
      );
      tokens.textContent = `${value}${tokens.dataset.tokenSuffix || ""}`;
    }
    const summary = row.querySelector("[data-live-phase-tools-summary]");
    if (summary) {
      summary.textContent = `${formatNumber(phaseUsage.tool_calls_total || 0)}${summary.dataset.toolSuffix || ""}`;
    }
    const list = row.querySelector("[data-live-phase-tools-list]");
    if (list) renderToolList(list, phaseUsage.tools || {});
  });
  document.querySelectorAll("[data-live-phase-graph-run-id]").forEach((node) => {
    if (node.dataset.livePhaseGraphRunId !== runId) return;
    const phaseUsage = usage.phase_usages?.[node.dataset.livePhaseGraphId] || {};
    node.textContent = `${formatNumber(phaseUsage.context_window_tokens ?? phaseUsage.total_tokens)} tokens`;
  });
}

function elapsedNode(startedAt, finishedAt = null) {
  const result = element("span", "elapsed", `elapsed ${runElapsed(startedAt, finishedAt)}`);
  if (startedAt && !finishedAt) result.dataset.started = String(startedAt);
  return result;
}

function metric(label, value, note = "") {
  const result = panel(null, "metric");
  append(result, element("div", "metric-label", label), element("div", "metric-value", value));
  if (note) append(result, element("div", "metric-note", note));
  return result;
}

function diagnostic(value, className = "") {
  const text = String(value || "").trim();
  if (!text.includes("\n") && text.length <= 240) return element("span", className, text || "—");
  const details = element("details", `diagnostic ${className}`.trim());
  const summary = element("summary", "diagnostic-summary", diagnosticSummary(text));
  append(details, summary, element("pre", "diagnostic-output", text));
  return details;
}

function renderOverview({ supportingOnly = false } = {}) {
  const fragment = document.createDocumentFragment();
  if (!supportingOnly) {
    append(fragment, heading("LIVE OPERATIONS", "Overview", "Authoritative server state with SSE-triggered refresh and polling recovery."));
    append(fragment, renderOverviewRunPulse());
  } else append(fragment, renderCurrentPhase());
  append(fragment, renderLifetimeStats());

  const eventsPanel = panel("Recent Signals");
  eventsPanel.dataset.liveRegion = "recent-signals";
  const feed = element("div", "event-feed");
  if (!state.recentEvents.length) append(feed, element("div", "empty-state", "Waiting for live SSE events."));
  for (const event of state.recentEvents) append(feed, eventLine(event));
  append(eventsPanel, feed);
  append(fragment, eventsPanel, renderRecentRuns());
  return fragment;
}

function renderLifetimeStats() {
  const stats = state.stats || {
    total_runs: 0, successful_runs: 0, failed_runs: 0, halted_runs: 0,
    repositories: 0, tickets: 0, duration_s: 0, context_tokens: 0,
    output_tokens: 0, total_tokens: 0, cost: 0, phase_executions: 0,
    tool_calls: 0, self_checks: 0, repeat_attempts: 0, model_loads: 0,
    model_load_duration_s: 0, models: [], phases: [], recent_runs: [],
  };
  const result = panel("Statistics", "statistics-panel");
  result.dataset.liveRegion = "statistics";
  const details = element("div", "statistics-grid");
  const outcomes = statCard("Run Outcomes", "outcome-panel stats-outcomes");
  const terminal = stats.successful_runs + stats.failed_runs + stats.halted_runs;
  const successPct = terminal ? (stats.successful_runs / terminal) * 100 : 0;
  const failedPct = terminal ? (stats.failed_runs / terminal) * 100 : 0;
  const ring = element("div", `outcome-ring${terminal ? "" : " empty"}`);
  ring.style.setProperty("--success", `${successPct}%`);
  ring.style.setProperty("--failed", `${successPct + failedPct}%`);
  append(ring, element("strong", "", terminal ? `${Math.round(successPct)}%` : "0"), element("span", "", terminal ? "success" : "runs"));
  const legend = element("div", "outcome-legend");
  for (const [tone, label, value] of [
    ["success", "Succeeded", stats.successful_runs],
    ["failed", "Failed", stats.failed_runs],
    ["halted", "Halted", stats.halted_runs],
  ]) {
    const row = element("div", "outcome-row");
    const dot = element("i", "outcome-dot"); dot.dataset.tone = tone;
    append(row, dot, element("span", "", label), element("strong", "mono", formatNumber(value)));
    legend.append(row);
  }
  append(outcomes, ring, legend);

  const tokenUsage = statCard("Token Usage", "stats-token-usage");
  const contextShare = stats.total_tokens ? (stats.context_tokens / stats.total_tokens) * 100 : 0;
  const tokenBar = element("div", "token-composition");
  tokenBar.style.setProperty("--context-share", `${contextShare}%`);
  append(tokenBar, element("span", "token-context"), element("span", "token-output"));
  const tokenLegend = element("div", "token-legend");
  append(
    tokenLegend,
    tokenLegendItem("context", "Context", stats.context_tokens),
    tokenLegendItem("output", "Output", stats.output_tokens),
  );
  append(tokenUsage, element("strong", "stats-total mono", formatNumber(stats.total_tokens)), element("span", "stats-total-label", "total tokens"), tokenBar, tokenLegend);

  const totals = statCard("Totals", "stats-totals");
  const totalsGrid = element("div", "lifetime-compact-grid");
  append(
    totalsGrid,
    compactLifetimeMetric("Runs", stats.total_runs),
    compactLifetimeMetric("Tool calls", stats.tool_calls),
    compactLifetimeMetric("Self-checks", stats.self_checks),
    compactLifetimeMetric("Repeat attempts", stats.repeat_attempts),
    compactLifetimeMetric("Model loads", stats.model_loads),
    compactLifetimeMetric("Model load time", formatDuration(stats.model_load_duration_s)),
    compactLifetimeMetric("Reported cost", formatMoney(stats.cost)),
    compactLifetimeMetric("Quill time", formatDuration(stats.duration_s)),
    compactLifetimeMetric("Repositories", stats.repositories),
    compactLifetimeMetric("Tickets", stats.tickets),
  );
  totals.append(totalsGrid);

  const trends = statCard("Recent Run Trends", "stats-trends");
  if (!stats.recent_runs.length) {
    append(trends, element("div", "empty-state", "No completed run history is available yet."));
  } else {
    const trendGrid = element("div", "trend-grid");
    append(
      trendGrid,
      sparkline("Tokens per run", "Total tokens", stats.recent_runs, "total_tokens", formatNumber),
      sparkline("Duration per run", "Elapsed time", stats.recent_runs, "duration_s", formatDuration),
    );
    trends.append(trendGrid);
  }

  const phases = statCard("Phase Time", "stats-phases");
  if (!stats.phases.length) {
    append(phases, element("div", "empty-state", "No phase history has been recorded yet."));
  } else {
    const maxDuration = Math.max(...stats.phases.map((item) => item.duration_s), 1);
    const phaseList = element("div", "phase-stat-list");
    for (const item of stats.phases.slice(0, 8)) append(phaseList, proportionalStat(item.label, item.duration_s, maxDuration, formatDuration(item.duration_s), `${formatNumber(item.executions)} executions`));
    phases.append(phaseList);
  }

  const models = statCard("Model Usage", "model-stats-panel stats-models");
  if (!stats.models.length) {
    append(models, element("div", "empty-state", "No model usage has been recorded yet."));
  } else {
    const maxTokens = Math.max(...stats.models.map((item) => item.total_tokens), 1);
    const list = element("div", "model-stat-list");
    for (const item of stats.models) {
      const row = element("div", "model-stat");
      const header = element("div", "model-stat-header");
      append(header, element("strong", "mono", item.model), element("span", "mono", formatNumber(item.total_tokens)));
      const track = element("div", "model-stat-track");
      const fill = element("div", "model-stat-fill");
      fill.style.setProperty("--share", `${Math.max(2, (item.total_tokens / maxTokens) * 100)}%`);
      track.append(fill);
      append(
        row,
        header,
        track,
        element("div", "model-stat-meta", `${formatNumber(item.calls)} calls · ${formatDuration(item.duration_s)} · ${formatMoney(item.cost)}`),
      );
      list.append(row);
    }
    models.append(list);
  }

  append(details, outcomes, tokenUsage, totals, trends, phases, models, renderOverviewQueueCard());
  result.append(details);
  return result;
}

function renderOverviewQueueCard() {
  const result = statCard("Queue", "stats-project-queue");
  result.dataset.liveRegion = "overview-queue";
  const title = result.querySelector("h3");
  const link = element("a", "stat-card-link", "Queue");
  link.href = "#/queue";
  title.replaceWith(link);
  const batches = state.projectQueue?.batches || [];
  if (!batches.length) {
    append(result, element("div", "compact-empty-state", "No tickets queued."));
    return result;
  }
  const list = element("div", "overview-queue-list");
  for (const batch of batches) {
    const header = element("div", "overview-queue-batch");
    append(
      header,
      element("strong", "mono", `BATCH ${batch.position}`),
      element("span", "muted", batch.repo),
      badge(batch.state),
    );
    list.append(header);
    for (const item of batch.items || []) {
      const row = element("div", "overview-queue-ticket");
      append(
        row,
        element("span", "mono", `${batch.position}.${item.position}`),
        element("strong", "mono", `#${item.ticket}`),
        element("span", "overview-queue-title", item.title),
      );
      list.append(row);
    }
  }
  result.append(list);
  return result;
}

function statCard(title, className = "") {
  const result = element("section", `stat-card ${className}`.trim());
  append(result, element("h3", "", title));
  return result;
}

function tokenLegendItem(tone, label, value) {
  const result = element("div", "token-legend-item");
  const dot = element("i", "token-legend-dot");
  dot.dataset.tone = tone;
  append(result, dot, element("span", "", label), element("strong", "mono", formatNumber(value)));
  return result;
}

function proportionalStat(label, value, maximum, display, note = "") {
  const result = element("div", "proportional-stat");
  const header = element("div", "proportional-stat-header");
  append(header, element("strong", "", label), element("span", "mono", display));
  const track = element("div", "proportional-stat-track");
  const fill = element("span", "proportional-stat-fill");
  fill.style.setProperty("--share", `${Math.max(2, (Number(value) / maximum) * 100)}%`);
  track.append(fill);
  append(result, header, track);
  if (note) append(result, element("small", "muted", note));
  return result;
}

function sparkline(label, yAxisLabel, points, field, formatter) {
  const result = element("div", "sparkline-card");
  const values = points.map((item) => Math.max(0, Number(item[field]) || 0));
  const maximum = Math.max(...values, 1);
  const width = 560;
  const height = 172;
  const yTicks = [maximum, maximum / 2, 0].map((value) => [value, formatter(value)]);
  const plot = {
    left: sparklineLeftMargin(yTicks.map(([, text]) => text)),
    right: 548,
    top: 12,
    bottom: 145,
  };
  const coordinates = values.map((value, index) => {
    const x = values.length === 1
      ? (plot.left + plot.right) / 2
      : plot.left + (index / (values.length - 1)) * (plot.right - plot.left);
    const y = plot.bottom - (value / maximum) * (plot.bottom - plot.top);
    return [x, y];
  });
  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${label}: ${yAxisLabel} by completed run, oldest to newest`,
  });
  for (const [value, text] of yTicks) {
    const y = plot.bottom - (value / maximum) * (plot.bottom - plot.top);
    const tick = svgElement("text", { x: plot.left - 8, y: y + 4, class: "sparkline-tick", "text-anchor": "end" });
    tick.textContent = text;
    append(svg, svgElement("line", { x1: plot.left, x2: plot.right, y1: y, y2: y, class: "sparkline-grid-line" }), tick);
  }
  const xTicks = values.length === 1 ? [[coordinates[0][0], "Run 1", "middle"]] : [
    [plot.left, "Run 1", "start"],
    [plot.right, `Run ${values.length}`, "end"],
  ];
  for (const [x, text, anchor] of xTicks) {
    const tick = svgElement("text", { x, y: plot.bottom + 17, class: "sparkline-tick", "text-anchor": anchor });
    tick.textContent = text;
    append(svg, svgElement("line", { x1: x, x2: x, y1: plot.bottom, y2: plot.bottom + 4, class: "sparkline-axis" }), tick);
  }
  append(svg, svgElement("line", { x1: plot.left, x2: plot.left, y1: plot.top, y2: plot.bottom, class: "sparkline-axis" }));
  const areaPoints = [`${coordinates[0][0]},${plot.bottom}`, ...coordinates.map(([x, y]) => `${x},${y}`), `${coordinates.at(-1)[0]},${plot.bottom}`].join(" ");
  append(
    svg,
    svgElement("polygon", { points: areaPoints, class: "sparkline-area" }),
    svgElement("polyline", { points: coordinates.map(([x, y]) => `${x},${y}`).join(" "), class: "sparkline-line" }),
  );
  if (values.length > 1) {
    const trendCoordinates = linearTrend(values).map((value, index) => [
      coordinates[index][0],
      plot.bottom - (Math.min(value, maximum) / maximum) * (plot.bottom - plot.top),
    ]);
    append(svg, svgElement("polyline", { points: trendCoordinates.map(([x, y]) => `${x},${y}`).join(" "), class: "sparkline-trend" }));
  }
  coordinates.forEach(([cx, cy], index) => {
    const dot = svgElement("circle", { cx, cy, r: 4, class: "sparkline-dot", "data-status": points[index].status });
    dot.append(svgElement("title"));
    dot.firstChild.textContent = `Run ${index + 1} · ${points[index].run_id} · ${points[index].status}: ${formatter(values[index])}`;
    svg.append(dot);
  });
  const latest = values.at(-1) || 0;
  const header = element("div", "sparkline-header");
  const legend = element("div", "sparkline-legend");
  append(
    legend,
    element("span", "sparkline-legend-item", "Per run"),
    element("span", "sparkline-legend-item trend", "Trend"),
  );
  append(header, element("strong", "", label), legend);
  append(result, header, svg, element("div", "sparkline-latest mono", `Latest ${formatter(latest)}`));
  return result;
}

function compactLifetimeMetric(label, value, note = "") {
  const result = element("div", "lifetime-compact-metric");
  append(result, element("span", "metric-label", label), element("strong", "mono", typeof value === "number" ? formatNumber(value) : value));
  if (note) append(result, element("small", "metric-note", note));
  return result;
}

function eventLine(event) {
  const row = element("div", "event-line");
  const type = String(event.type || "event").replaceAll("_", " ");
  const detail = [event.phase, event.label, event.verdict, event.reason].filter(Boolean).join(" · ");
  const time = formatTime(event.ts);
  const timeNode = element("time", "", time.label === "—" ? "now" : time.label.split(", ").at(-1));
  if (time.iso) timeNode.dateTime = time.iso;
  append(row, element("span", "badge", type), element("span", "truncate", detail || event.run_id || "Quill state changed"), timeNode);
  return row;
}

function renderRecentRuns() {
  const result = panel("Recent Runs");
  result.dataset.liveRegion = "recent-runs";
  if (!state.overviewRuns.length) {
    append(result, element("div", "empty-state", "No recorded runs are available."));
    return result;
  }
  const controls = element("div", "run-pagination");
  const page = Math.floor(state.overviewRunPage.offset / state.overviewRunPage.limit) + 1;
  const previous = button("Previous", "secondary small", async () => {
    state.overviewRunPage.offset = Math.max(0, state.overviewRunPage.offset - state.overviewRunPage.limit);
    await refreshRuns();
  });
  previous.disabled = state.overviewRunPage.offset === 0 || loading("runs");
  const next = button("Next", "secondary small", async () => {
    state.overviewRunPage.offset += state.overviewRunPage.limit;
    await refreshRuns();
  });
  next.disabled = !state.overviewRunPage.hasMore || loading("runs");
  append(controls, previous, element("span", "muted mono", `Page ${page} · 25 runs`), next);
  append(result, runsTable(state.overviewRuns), controls);
  return result;
}

function runsTable(runs, selectable = false) {
  const wrap = element("div", "table-wrap");
  const table = element("table");
  const head = element("thead");
  const headRow = element("tr");
  if (selectable) {
    const deletableRuns = runs.filter((run) => !["queued", "running", "needs_decision"].includes(run.status));
    const selectAllCell = element("th", "run-select-cell");
    const selectAll = element("input");
    selectAll.type = "checkbox";
    selectAll.setAttribute("aria-label", "Select all visible runs");
    selectAll.checked = deletableRuns.length > 0 && deletableRuns.every((run) => state.selectedRunIds.has(run.run_id));
    selectAll.indeterminate = !selectAll.checked && deletableRuns.some((run) => state.selectedRunIds.has(run.run_id));
    selectAll.disabled = !deletableRuns.length;
    selectAll.addEventListener("change", () => {
      state.selectedRunIds = selectAll.checked
        ? new Set(deletableRuns.map((run) => run.run_id))
        : new Set();
      render();
    });
    selectAllCell.append(selectAll);
    headRow.append(selectAllCell);
  }
  for (const title of ["Run", "Status", "Repository", "Ticket", "Workflow", "Phase", "Total Run Time", "Updated"]) append(headRow, element("th", "", title));
  append(head, headRow);
  const body = element("tbody");
  for (const run of runs) {
    const row = element("tr");
    if (selectable) {
      const selectCell = element("td", "run-select-cell");
      const selectRun = element("input");
      selectRun.type = "checkbox";
      selectRun.disabled = ["queued", "running", "needs_decision"].includes(run.status);
      selectRun.checked = state.selectedRunIds.has(run.run_id);
      selectRun.setAttribute("aria-label", `Select ${run.run_id}`);
      selectRun.addEventListener("change", () => {
        if (selectRun.checked) state.selectedRunIds.add(run.run_id);
        else state.selectedRunIds.delete(run.run_id);
        render();
      });
      selectCell.append(selectRun);
      row.append(selectCell);
    }
    const runCell = element("td");
    const link = element("a", "run-link", run.run_id);
    link.href = `#/runs/${encodeURIComponent(run.run_id)}`;
    append(runCell, link);
    const statusCell = element("td");
    statusCell.append(badge(run.status));
    const updated = formatTime(run.updated_at);
    const updatedNode = element("time", "", updated.label);
    if (updated.iso) updatedNode.title = updated.iso;
    const terminal = ["done", "failed", "halted"].includes(run.status);
    const durationNode = element("span", "mono", runElapsed(run.started_at, terminal ? run.updated_at : null));
    if (run.started_at && !terminal) durationNode.dataset.liveRunStarted = String(run.started_at);
    const durationCell = element("td");
    durationCell.append(durationNode);
    append(
      row,
      runCell,
      statusCell,
      element("td", "", run.repo || "—"),
      element("td", "mono", run.ticket ?? "—"),
      element("td", "", `${run.workflow || "legacy ticket"}${run.pr_number ? ` · PR #${run.pr_number}` : ""}`),
      element("td", "", run.phase_label || run.phase || "—"),
      durationCell,
      element("td", "", updatedNode.textContent),
    );
    body.append(row);
  }
  append(table, head, body);
  wrap.append(table);
  return wrap;
}

function renderRuns() {
  const fragment = document.createDocumentFragment();
  append(fragment, heading("RUN CONTROL", "Runs", "Start committed repository workflows, filter history, and inspect live or persisted telemetry."));
  if (state.route.id) {
    append(fragment, renderRunPulse(state.runDetail || state.runs.find((run) => run.run_id === state.route.id)));
    append(fragment, renderPhaseGraphPanel(state.runDetail || state.runs.find((run) => run.run_id === state.route.id)));
    append(fragment, renderRunInspector());
  } else {
    append(fragment, renderStartRun());
    append(fragment, renderRunFilters());
    append(fragment, renderRunsListing());
  }
  return fragment;
}

function renderRunsListing() {
  const listing = panel("Run History");
  listing.dataset.liveRegion = "runs-listing";
  if (loading("runs") && !state.runs.length) append(listing, element("div", "loading-bar"));
  if (state.errors.runs) append(listing, element("div", "error-state", state.errors.runs));
  else if (state.runs.length) {
    append(listing, renderRunBulkActions(), runsTable(state.runs, true), renderRunPagination());
  } else append(listing, element("div", "empty-state", "No runs match these filters."));
  return listing;
}

function renderQueue() {
  const fragment = document.createDocumentFragment();
  append(fragment, heading(
    "AUTOMATED DELIVERY",
    "Queue",
    "Submit ordered ticket batches. Quill advances only after the current ticket's pull request is merged.",
  ));
  append(fragment, renderProjectQueueOrder(), renderQueueCandidates());
  return fragment;
}

function renderProjectQueueOrder() {
  const result = panel("Execution Order", "project-queue-order-panel");
  result.dataset.liveRegion = "project-queue-order";
  if (loading("project-queue") && !(state.projectQueue?.batches || []).length) {
    append(result, element("div", "loading-bar"));
  }
  if (state.errors["project-queue"]) {
    append(result, element("div", "error-state", state.errors["project-queue"]));
    return result;
  }
  const batches = state.projectQueue?.batches || [];
  if (!batches.length) {
    append(result, element("div", "empty-state queue-empty-state", "No ticket batches are queued."));
    return result;
  }
  const list = element("div", "project-batch-list");
  for (const batch of batches) {
    const batchPanel = element("section", "project-batch");
    batchPanel.dataset.state = batch.state;
    const header = element("div", "project-batch-header");
    append(
      header,
      element("strong", "mono", `BATCH ${batch.position}`),
      element("span", "mono muted", batch.repo),
      badge(batch.state),
      element("time", "mono muted", formatTime(batch.submitted_at).label),
    );
    batchPanel.append(header);
    if (batch.error) append(batchPanel, diagnostic(batch.error, "notice danger"));
    const wrap = element("div", "table-wrap project-queue-table-wrap");
    const table = element("table", "project-queue-table");
    const head = element("thead");
    const headRow = element("tr");
    for (const title of ["Order", "Ticket", "Title", "Epic", "Board", "State", "Run", "PR"]) {
      append(headRow, element("th", "", title));
    }
    head.append(headRow);
    const body = element("tbody");
    for (const item of batch.items || []) {
      const row = element("tr");
      if (["paused", "failed", "halted"].includes(item.state)) row.classList.add("project-queue-blocked");
      const runCell = element("td");
      if (item.run_id) {
        const runLink = element("a", "run-link", item.run_id);
        runLink.href = `#/runs/${encodeURIComponent(item.run_id)}`;
        runLink.addEventListener("click", () => { state.runTab = "breakdown"; });
        runCell.append(runLink);
      } else runCell.textContent = "—";
      append(
        row,
        element("td", "mono", `${batch.position}.${item.position}`),
        element("td", "mono", `#${item.ticket}`),
        element("td", "", item.title),
        element("td", "", item.epic_number ? `#${item.epic_number} ${item.epic_title || ""}`.trim() : "Standalone tickets"),
        element("td", "", item.board_status || "—"),
        element("td", "", item.state || "—"),
        runCell,
        element("td", "mono", item.pr_number ? `#${item.pr_number}` : "—"),
      );
      body.append(row);
      if (item.error) {
        const errorRow = element("tr", "project-queue-error-row");
        const cell = element("td");
        cell.colSpan = 8;
        cell.append(diagnostic(item.error, "notice danger"));
        errorRow.append(cell);
        body.append(errorRow);
      }
    }
    append(table, head, body);
    wrap.append(table);
    batchPanel.append(wrap);
    list.append(batchPanel);
  }
  result.append(list);
  return result;
}

function renderQueueCandidates() {
  const result = panel("Project Tickets", "project-ticket-panel");
  result.dataset.liveRegion = "queue-candidates";
  const repositories = queueCapableRepositories(state.github.repositories);
  const repoField = choiceField(
    "Repository",
    repositories.map((repository) => ({
      value: repository.name,
      label: `${repository.name} · ${repository.project_board}`,
    })),
    state.queuePage.repo,
    "No project-backed repositories",
  );
  repoField.input.addEventListener("change", async () => {
    state.queuePage.repo = repoField.input.value;
    state.queuePage.groups = [];
    state.queuePage.selected.clear();
    state.queuePage.result = null;
    render();
    await refreshQueueCandidates(state.queuePage.repo);
  });
  const controls = element("div", "queue-candidate-controls");
  controls.append(repoField.label);
  result.append(controls);
  if (!repositories.length && !loading("github-repositories")) {
    append(result, element("div", "empty-state", "No repository has a configured GitHub Project board."));
    return result;
  }
  if (loading("queue-candidates")) append(result, element("div", "loading-bar"));
  if (state.errors["queue-candidates"]) {
    append(result, element("div", "error-state", state.errors["queue-candidates"]));
    return result;
  }
  const groups = (state.queuePage.groups || []).filter((group) => (group.tickets || []).length);
  if (!groups.length && !loading("queue-candidates")) {
    append(result, element("div", "empty-state", "No project tickets are available for this repository."));
    return result;
  }
  if (groups.length) {
    const wrap = element("div", "table-wrap queue-candidate-table-wrap");
    const table = element("table", "queue-candidate-table");
    const head = element("thead");
    const headRow = element("tr");
    for (const title of ["Select", "Ticket", "Title", "Labels", "Status", "Availability"]) {
      append(headRow, element("th", "", title));
    }
    head.append(headRow);
    const body = element("tbody");
    for (const group of groups) {
      const groupRow = element("tr", "queue-epic-row");
      const selectCell = element("td");
      const selector = element("input");
      selector.type = "checkbox";
      const selection = groupSelectionState(group, state.queuePage.selected);
      selector.checked = selection.checked;
      selector.indeterminate = selection.indeterminate;
      selector.disabled = selection.disabled;
      selector.setAttribute("aria-label", `Select all tickets in ${group.epic_title || "Standalone tickets"}`);
      selector.addEventListener("change", () => {
        for (const ticket of group.tickets || []) {
          if (!ticket.selectable) continue;
          const key = String(ticket.number);
          if (selector.checked) state.queuePage.selected.add(key);
          else state.queuePage.selected.delete(key);
        }
        updateQueueCandidatesRegion();
      });
      selectCell.append(selector);
      const title = group.epic_number
        ? `#${group.epic_number} ${group.epic_title}`
        : "Standalone tickets";
      const groupTitle = element("td", "queue-epic-title", title);
      groupTitle.colSpan = 5;
      append(groupRow, selectCell, groupTitle);
      body.append(groupRow);
      for (const ticket of [...(group.tickets || [])].sort((left, right) => Number(left.number) - Number(right.number))) {
        const row = element("tr", "queue-ticket-row");
        const ticketSelectCell = element("td");
        const ticketSelector = element("input");
        ticketSelector.type = "checkbox";
        ticketSelector.checked = state.queuePage.selected.has(String(ticket.number));
        ticketSelector.disabled = !ticket.selectable;
        ticketSelector.setAttribute("aria-label", `Select ticket #${ticket.number}`);
        ticketSelector.addEventListener("change", () => {
          if (ticketSelector.checked) state.queuePage.selected.add(String(ticket.number));
          else state.queuePage.selected.delete(String(ticket.number));
          updateQueueCandidatesRegion();
        });
        ticketSelectCell.append(ticketSelector);
        append(
          row,
          ticketSelectCell,
          element("td", "mono", `#${ticket.number}`),
          element("td", "", ticket.title),
          element("td", "", (ticket.labels || []).join(", ") || "—"),
          element("td", "", ticket.status || "—"),
          element("td", ticket.selectable ? "" : "muted", ticket.selectable ? "Ready" : ticket.reason || "Unavailable"),
        );
        body.append(row);
      }
    }
    append(table, head, body);
    wrap.append(table);
    result.append(wrap);
  }
  const actions = element("div", "queue-candidate-actions");
  if (state.queuePage.result) append(actions, element("span", "notice", state.queuePage.result));
  const add = button("Add To Queue", "primary", submitQueueBatch);
  add.disabled = state.queuePage.selected.size === 0 || loading("queue-submit");
  append(actions, element("span", "mono muted", `${state.queuePage.selected.size} selected`), add);
  result.append(actions);
  return result;
}

function updateQueueCandidatesRegion() {
  if (state.route.section !== "queue") return;
  const mounted = document.querySelector('[data-live-region="queue-candidates"]');
  if (!mounted) return;
  const next = renderQueueCandidates();
  mounted.replaceWith(next);
}

async function submitQueueBatch() {
  const repo = state.queuePage.repo;
  const tickets = [...state.queuePage.selected].map(Number).sort((left, right) => left - right);
  if (!repo || !tickets.length) return;
  setLoading("queue-submit", true);
  updateQueueCandidatesRegion();
  try {
    const response = await QuillApi.addProjectQueueBatch(repo, tickets);
    const results = response.results || [];
    const queued = results.filter((result) => result.queued);
    const failed = results.filter((result) => !result.queued);
    for (const item of queued) state.queuePage.selected.delete(String(item.ticket));
    const summary = `${queued.length} ticket${queued.length === 1 ? "" : "s"} moved to Queue.`;
    state.queuePage.result = failed.length
      ? `${summary} ${failed.map((item) => `#${item.ticket}: ${item.reason || "not queued"}`).join("; ")}`
      : summary;
    toast(summary, failed.length ? "warning" : "success");
    await Promise.all([
      refreshProjectQueue({ quiet: true }),
      refreshQueueCandidates(repo, { quiet: true }),
    ]);
  } catch (error) {
    handleError(error, "queue-candidates");
  } finally {
    setLoading("queue-submit", false);
    updateQueueCandidatesRegion();
  }
}

function renderStartRun() {
  const result = panel("Start a Run", "start-run-panel");
  if (state.errors.github) append(result, element("div", "error-state", state.errors.github));
  const form = element("form", "start-run-form");
  const repo = choiceField(
    `Repository · ${state.github.login || "GitHub"}`,
    state.github.repositories.map((item) => ({
      value: item.name,
      label: `${item.name} · ${item.visibility.toLowerCase()}`,
    })),
    state.runDraft.repo,
    loading("github-repositories") ? "Loading repositories…" : "No repositories found",
  );
  const workType = choiceField(
    "Work type",
    state.github.work_types.map((value) => ({ value, label: value })),
    state.runDraft.work_type,
    loading("github-issues") ? "Loading conventions…" : "No convention found",
  );
  workType.input.disabled = state.github.work_types.length <= 1;
  const ticket = choiceField(
    "Ticket",
    state.github.issues.map((issue) => ({
      value: issue.number,
      label: `#${issue.number} · ${issue.title}`,
    })),
    state.runDraft.ticket,
    loading("github-issues") ? "Loading open tickets…" : "No open tickets",
  );
  const workflow = choiceField(
    "Workflow",
    state.github.workflows.map((item) => ({ value: item.id, label: item.label })),
    state.runDraft.workflow,
    loading("github-workflows") ? "Loading workflows…" : "No workflows available",
  );
  const branch = field(state.runDraft.mode === "create" ? "Generated branch" : "Existing PR branch", "text", state.runDraft.branch);
  branch.input.readOnly = true;
  repo.input.required = true;
  workType.input.required = true;
  branch.input.required = true;
  ticket.input.required = true;
  repo.input.addEventListener("change", async () => {
    state.runDraft.repo = repo.input.value;
    state.runDraft.ticket = "";
    state.runDraft.branch = "";
    state.github.allIssues = [];
    state.github.issues = [];
    state.github.allWorkTypes = [];
    state.github.work_types = [];
    state.github.models = [];
    state.github.excludedIssueLabels = [];
    state.github.updateTarget = null;
    await Promise.all([refreshGitHubIssues(repo.input.value), refreshGitHubWorkflows(repo.input.value)]);
  });
  workType.input.addEventListener("change", async () => {
    state.runDraft.work_type = workType.input.value;
    const branchRefresh = refreshDraftBranch();
    render();
    await branchRefresh;
  });
  ticket.input.addEventListener("change", async () => {
    state.runDraft.ticket = ticket.input.value;
    applyIssueLabelFilter();
    const branchRefresh = refreshDraftBranch();
    render();
    await branchRefresh;
  });
  workflow.input.addEventListener("change", async () => {
    state.runDraft.workflow = workflow.input.value;
    const selected = state.github.workflows.find((item) => item.id === state.runDraft.workflow);
    state.runDraft.mode = selected?.mode || "create";
    state.runDraft.branch = "";
    state.github.updateTarget = null;
    state.runDraft.override_models = false;
    state.runDraft.model_overrides = {};
    delete state.errors["github-update-target"];
    const branchRefresh = refreshDraftBranch();
    render();
    await branchRefresh;
  });

  const submit = element("button", "button", loading("start-run") ? "Queueing…" : "Start run");
  submit.type = "submit";
  const updateReady = state.runDraft.mode === "create" || state.github.updateTarget?.available;
  submit.disabled = loading("start-run") || loading("github-update-target") || !state.runDraft.ticket || !state.runDraft.branch || !state.runDraft.workflow || !updateReady;
  append(form, repo.label, ticket.label, workflow.label);
  if (state.runDraft.mode === "create") append(form, workType.label);
  append(form, branch.label);
  const selectedWorkflow = state.github.workflows.find((item) => item.id === state.runDraft.workflow);
  const modelPhases = selectedWorkflow?.phases || [];
  if (modelPhases.length && state.github.models.length) {
    const overrideToggle = element("label", "checkbox-field model-override-toggle");
    const overrideInput = element("input");
    overrideInput.type = "checkbox";
    overrideInput.checked = state.runDraft.override_models;
    append(overrideToggle, overrideInput, element("span", "", "Override models for this run"));
    overrideInput.addEventListener("change", () => {
      state.runDraft.override_models = overrideInput.checked;
      state.runDraft.model_overrides = overrideInput.checked
        ? Object.fromEntries(modelPhases.map((phase) => [phase.id, phase.model]))
        : {};
      render();
    });
    append(form, overrideToggle);
    if (state.runDraft.override_models) {
      const overridePanel = element("section", "model-override-panel");
      append(overridePanel, element("h3", "", "Run Model Overrides"));
      const chosenModels = modelPhases.map((phase) => state.runDraft.model_overrides[phase.id] || phase.model);
      const commonModel = chosenModels.every((model) => model === chosenModels[0]) ? chosenModels[0] : "";
      const allModels = choiceField(
        "All phases",
        [{ value: "", label: "Keep phase selections" }, ...state.github.models.map((model) => ({ value: model, label: model }))],
        commonModel,
        "No models configured",
      );
      allModels.input.addEventListener("change", () => {
        if (!allModels.input.value) return;
        state.runDraft.model_overrides = Object.fromEntries(modelPhases.map((phase) => [phase.id, allModels.input.value]));
        render();
      });
      overridePanel.append(allModels.label);
      const phaseGrid = element("div", "model-override-grid");
      for (const phase of modelPhases) {
        const phaseModel = choiceField(
          phase.label,
          state.github.models.map((model) => ({ value: model, label: model })),
          state.runDraft.model_overrides[phase.id] || phase.model,
          "No models configured",
        );
        phaseModel.input.addEventListener("change", () => {
          const linked = phase.parallel_group
            ? modelPhases.filter((candidate) => candidate.parallel_group === phase.parallel_group)
            : [phase];
          state.runDraft.model_overrides = {
            ...state.runDraft.model_overrides,
            ...Object.fromEntries(linked.map((candidate) => [candidate.id, phaseModel.input.value])),
          };
          render();
        });
        phaseGrid.append(phaseModel.label);
      }
      overridePanel.append(phaseGrid);
      form.append(overridePanel);
    }
  }
  if (state.runDraft.mode !== "create" && state.errors["github-update-target"]) {
    append(form, element("div", "error-state start-run-message", state.errors["github-update-target"]));
  }
  if (state.runDraft.mode !== "create" && state.github.updateTarget) {
    const target = state.github.updateTarget;
    append(form, element("p", `${target.available ? "notice" : "error-state"} start-run-message`, target.available
      ? state.runDraft.mode === "review"
        ? `PR #${target.pr_number} · review head ${target.head_sha.slice(0, 12)}`
        : `PR #${target.pr_number} · ${target.feedback_count} new feedback item(s) · ${target.head_sha.slice(0, 12)}`
      : target.reason));
  }
  const actions = element("div", "start-run-actions");
  append(actions, submit);
  append(form, actions);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    Object.assign(state.runDraft, {
      repo: repo.input.value.trim(),
      branch: branch.input.value.trim(),
      ticket: ticket.input.value,
      work_type: workType.input.value,
      mode: state.runDraft.mode,
      workflow: workflow.input.value,
    });
    setLoading("start-run", true);
    render();
    try {
      const run = await QuillApi.start({
        repo: state.runDraft.repo,
        branch: state.runDraft.branch,
        ticket: Number(state.runDraft.ticket),
        mode: state.runDraft.mode,
        workflow: state.runDraft.workflow,
        generated_branch: state.runDraft.mode === "create",
        model_overrides: state.runDraft.override_models ? state.runDraft.model_overrides : {},
      });
      toast("Run started.", "success");
      location.hash = `#/runs/${encodeURIComponent(run.run_id)}`;
      await refreshRuns();
    } catch (error) {
      handleError(error, "start-run");
    } finally {
      setLoading("start-run", false);
      render();
    }
  });
  result.append(form);
  return result;
}

function renderRunFilters() {
  const result = panel(null);
  const form = element("div", "field-row run-filter-row");
  const repositoryNames = [...new Set([
    ...state.github.repositories.map((item) => item.name),
    ...state.runFacets.map((run) => run.repo).filter(Boolean),
  ])].sort();
  const repo = choiceField(
    "Repository",
    [{ value: "", label: "Any repository" }, ...repositoryNames.map((name) => ({ value: name, label: name }))],
    state.runFilters.repo,
    "No repositories found",
  );
  const tickets = [...new Set(
    state.runFacets
      .filter((run) => !state.runFilters.repo || run.repo === state.runFilters.repo)
      .map((run) => run.ticket)
      .filter((ticketNumber) => ticketNumber !== null && ticketNumber !== undefined),
  )].sort((left, right) => Number(right) - Number(left));
  const ticket = choiceField(
    "Ticket",
    [{ value: "", label: "Any ticket" }, ...tickets.map((number) => ({ value: number, label: `#${number}` }))],
    state.runFilters.ticket,
    "No recorded tickets",
  );
  const status = selectField("Status", ["", "queued", "running", "needs_decision", "done", "failed", "halted"], state.runFilters.status);

  const applyFilters = async () => {
    state.runFilters.offset = 0;
    state.runDetail = null;
    await refreshRuns();
  };
  repo.input.addEventListener("change", async () => {
    state.runFilters.repo = repo.input.value;
    state.runFilters.ticket = "";
    await applyFilters();
  });
  ticket.input.addEventListener("change", async () => {
    state.runFilters.ticket = ticket.input.value;
    await applyFilters();
  });
  status.input.addEventListener("change", async () => {
    state.runFilters.status = status.input.value;
    await applyFilters();
  });
  append(form, repo.label, ticket.label, status.label);
  result.append(form);
  return result;
}

function renderRunPagination() {
  const controls = element("div", "run-pagination");
  const page = Math.floor(state.runPage.offset / state.runPage.limit) + 1;
  const previous = button("Previous", "secondary small", async () => {
    state.runFilters.offset = Math.max(0, state.runPage.offset - state.runPage.limit);
    await refreshRuns();
  });
  previous.disabled = state.runPage.offset === 0 || loading("runs");
  const next = button("Next", "secondary small", async () => {
    state.runFilters.offset = state.runPage.offset + state.runPage.limit;
    await refreshRuns();
  });
  next.disabled = !state.runPage.hasMore || loading("runs");
  append(controls, previous, element("span", "muted mono", `Page ${page} · up to 200 runs`), next);
  return controls;
}

function renderRunBulkActions() {
  const actions = element("div", "run-bulk-actions");
  const count = state.selectedRunIds.size;
  append(actions, element("span", "muted", count ? `${count} selected` : "Select runs to delete"));
  const remove = button(
    loading("delete-runs") ? "Deleting…" : `Delete selected${count ? ` (${count})` : ""}`,
    "danger small",
    async () => {
      const runIds = [...state.selectedRunIds];
      if (!runIds.length) return;
      if (!await confirmAction(
        "Delete run history",
        `Delete ${runIds.length} selected run${runIds.length === 1 ? "" : "s"} and all saved artifacts? This cannot be undone.`,
      )) return;
      setLoading("delete-runs", true);
      render();
      try {
        const result = await QuillApi.deleteRuns(runIds);
        state.selectedRunIds.clear();
        toast(`${result.deleted.length} run${result.deleted.length === 1 ? "" : "s"} deleted.`, "success");
        await refreshRuns({ quiet: true, includeInspector: false });
      } catch (error) {
        handleError(error, "runs");
      } finally {
        setLoading("delete-runs", false);
        render();
      }
    },
  );
  remove.disabled = !count || loading("delete-runs");
  actions.append(remove);
  return actions;
}

function field(labelText, type = "text", value = "", placeholder = "") {
  const label = element("label");
  const input = element("input");
  input.type = type;
  input.value = value ?? "";
  input.placeholder = placeholder;
  if (type === "number") input.min = "1";
  append(label, element("span", "", labelText), input);
  return { label, input };
}

function selectField(labelText, options, selected) {
  const label = element("label");
  const input = element("select");
  for (const value of options) {
    const option = element("option", "", value === "" ? "Any" : String(value).replaceAll("_", " "));
    option.value = String(value);
    input.append(option);
  }
  input.value = String(selected ?? "");
  append(label, element("span", "", labelText), input);
  return { label, input };
}

function choiceField(labelText, options, selected, emptyLabel) {
  const label = element("label");
  const input = element("select");
  if (!options.length) {
    const option = element("option", "", emptyLabel);
    option.value = "";
    input.append(option);
    input.disabled = true;
  } else {
    for (const item of options) {
      const option = element("option", "", item.label);
      option.value = String(item.value);
      input.append(option);
    }
  }
  input.value = String(selected ?? "");
  append(label, element("span", "", labelText), input);
  return { label, input };
}

function checkboxField(labelText, checked = false) {
  const label = element("label", "checkbox-field");
  const input = element("input");
  input.type = "checkbox";
  input.checked = checked;
  append(label, input, element("span", "", labelText));
  return { label, input };
}

function renderRunInspector() {
  const result = panel(null);
  result.dataset.liveRegion = "run-inspector";
  if (loading("runs") && !state.runDetail) {
    append(result, element("div", "loading-bar"), element("div", "empty-state", "Loading run telemetry…"));
    return result;
  }
  if (state.errors.runs) {
    append(result, element("div", "error-state", state.errors.runs));
    return result;
  }
  if (!state.runDetail) {
    append(result, element("div", "empty-state", "Run details are unavailable."));
    return result;
  }
  const tabs = element("div", "tabs");
  tabs.setAttribute("role", "tablist");
  for (const [id, label] of [["status", "Status & history"], ["breakdown", "Breakdown"], ["artifacts", `Artifacts (${state.artifacts.length})`]]) {
    const tab = element("button", "tab", label);
    tab.dataset.runTab = id;
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(state.runTab === id));
    tab.addEventListener("click", () => {
      state.runTab = id;
      updateRunInspectorRegion({ force: true });
    });
    tabs.append(tab);
  }
  result.append(tabs);
  const content = element("div", "run-inspector-content");
  content.dataset.runInspectorContent = "true";
  content.append(renderRunInspectorContent());
  result.append(content);
  return result;
}

function renderRunInspectorContent() {
  if (state.runTab === "breakdown") return renderBreakdown();
  if (state.runTab === "artifacts") return renderArtifacts();
  return renderRunStatus();
}

function updateRunInspectorRegion({ force = false } = {}) {
  const mounted = document.querySelector('[data-live-region="run-inspector"]');
  if (!mounted) return;
  const content = mounted.querySelector("[data-run-inspector-content]");
  const active = document.activeElement;
  if (!force && content && (content.contains(active) || (openSelect && content.contains(openSelect)))) {
    state.pendingInspectorRefresh = true;
    return;
  }
  state.pendingInspectorRefresh = false;
  const next = renderRunInspector();
  const nextContent = next.querySelector("[data-run-inspector-content]");
  if (!content || !nextContent) {
    mounted.replaceChildren(...next.childNodes);
    updateElapsed();
    return;
  }
  const openDiagnostics = new Set([...content.querySelectorAll("details[open] summary")]
    .map((summary) => summary.textContent));
  for (const nextTab of next.querySelectorAll("[data-run-tab]")) {
    const currentTab = mounted.querySelector(`[data-run-tab="${nextTab.dataset.runTab}"]`);
    if (!currentTab) continue;
    currentTab.textContent = nextTab.textContent;
    currentTab.setAttribute("aria-selected", nextTab.getAttribute("aria-selected"));
  }
  content.replaceChildren(...nextContent.childNodes);
  for (const details of content.querySelectorAll("details")) {
    if (openDiagnostics.has(details.querySelector("summary")?.textContent)) details.open = true;
  }
  updateElapsed();
  if (state.runDetail?.run_id) updateProgressRegions(state.runDetail.run_id);
}

function flushPendingInspectorRefresh() {
  if (state.pendingInspectorRefresh) updateRunInspectorRegion();
}

function renderRunStatus() {
  const run = state.runDetail;
  const fragment = document.createDocumentFragment();
  const actions = element("div", "button-row");
  if (canStopRun(run.status)) {
    append(actions, button("Stop run", "danger", async () => {
      if (!(await confirmAction("Stop run", `Stop ${run.run_id} now?`))) return;
      await mutation(() => QuillApi.stop(run.run_id), "Stop requested");
      await refreshRuns();
    }));
  }
  if (run.pr_url && safeExternalUrl(run.pr_url)) {
    const link = element("a", "button secondary", "Open pull request");
    link.href = safeExternalUrl(run.pr_url);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    actions.append(link);
  }
  append(fragment, actions, detailGrid([
    ["Run ID", run.run_id], ["Status", run.status], ["Repository", run.repo], ["Branch", run.branch],
    ["Ticket", run.ticket], ["Workflow", run.workflow || "legacy ticket workflow"],
    ["Mode", run.mode || "historical"], ["Pull request", run.pr_number ? `#${run.pr_number}` : "—"],
    ["Restarted from", run.source_run_id ? `${run.source_run_id} · ${run.start_phase}` : "—"],
    ["Phase", run.phase_label || run.phase],
    ["Current activity", run.activity_label || run.activity],
    ["Attempt", run.max_attempts ? `${run.attempt}/${run.max_attempts}` : "—"],
    ["Queued", formatTime(run.queued_at).label], ["Started", formatTime(run.started_at).label],
    ["Updated", formatTime(run.updated_at).label],
  ]));
  if (run.error) append(fragment, diagnostic(runFailureMessage(run), "notice danger"));
  if (canAnswerRun(run.status)) append(fragment, renderDecisionForm(run));
  const historyPanel = element("div", "run-status-history");
  append(historyPanel, element("h3", "", "Phase history"));
  const persistedHistory = (state.breakdown?.phase_executions || []).map((item) => {
    const { call_number: attempt, ...execution } = item;
    return {
      ...execution,
      attempt,
      tools: item.tool_calls_by_name,
      reason: item.rejection_reason,
    };
  });
  const history = persistedHistory.length ? persistedHistory : (run.history || []);
  if (history.length) append(historyPanel, historyTable(history, run));
  else append(historyPanel, element("div", "empty-state", "No phase history is available for this run."));
  append(fragment, historyPanel);
  return fragment;
}

function detailGrid(items) {
  const list = element("dl", "detail-list");
  for (const [term, value] of items) {
    const item = element("div", "detail");
    append(item, element("dt", "", term), element("dd", "", value ?? "—"));
    list.append(item);
  }
  return list;
}

function renderDecisionForm(run) {
  const form = element("form", "panel-stack");
  append(form, element("p", "notice warning", run.question || "This run is waiting for an operator decision."));
  const answer = field("Answer", "text", state.decisionDraft, "Enter the decision sent back to the pipeline");
  answer.input.addEventListener("input", () => { state.decisionDraft = answer.input.value; });
  const submit = element("button", "button warning", "Send decision");
  submit.type = "submit";
  append(form, answer.label, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!answer.input.value.trim()) return toast("A nonblank decision is required", "warning");
    submit.disabled = true;
    const result = await mutation(() => QuillApi.decide(run.run_id, answer.input.value.trim()), "Decision sent");
    if (result) state.decisionDraft = "";
    await refreshRuns();
  });
  return form;
}

function historyTable(history, run = null) {
  const wrap = element("div", "table-wrap");
  const table = element("table");
  const head = element("tr");
  const restartable = Boolean(run?.run_id && state.restartOptions?.eligible);
  const titles = ["Phase", "Type", "Attempt", "Verdict", "Model", "Duration", "Tools", "Reason"];
  if (restartable) titles.push("Restart");
  for (const title of titles) append(head, element("th", "", title));
  const thead = element("thead"); thead.append(head);
  const body = element("tbody");
  for (const item of history) {
    const row = element("tr");
    const verdict = element("td"); verdict.append(badge(item.verdict || "incomplete"));
    append(row,
      element("td", "", item.label || item.phase), element("td", "", item.phase_type || "—"),
      element("td", "mono", item.attempt ?? "—"), verdict, element("td", "mono", item.model || "—"),
      element("td", "mono", item.duration_s == null ? "—" : formatDuration(item.duration_s)),
      element("td", "mono", formatNumber(Object.values(item.tools || {}).reduce((sum, value) => sum + Number(value), 0))),
      (() => { const cell = element("td"); cell.append(diagnostic(item.reason)); return cell; })());
    if (restartable) row.append(restartPhaseCell(run.run_id, item));
    body.append(row);
  }
  append(table, thead, body); wrap.append(table); return wrap;
}

function renderBreakdown() {
  const breakdown = state.breakdown;
  const fragment = document.createDocumentFragment();
  if (!breakdown) {
    append(fragment, element("div", "empty-state", "No breakdown is available."));
    return fragment;
  }
  const selectedRun = state.runs.find((run) => run.run_id === state.route.id);
  const tableRun = selectedRun || state.runDetail
    ? {
        ...(state.runDetail || {}),
        ...(selectedRun || {}),
        phase_graph: selectedRun?.phase_graph || state.runDetail?.phase_graph,
      }
    : null;
  const liveUsage = selectedRun && ["running", "needs_decision"].includes(selectedRun.status)
    ? state.liveUsage[selectedRun.run_id]
    : null;
  const usage = liveUsage || breakdown.cumulative_usage || {};
  const terminal = ["done", "failed", "halted"].includes(breakdown.status);
  const executions = breakdown.phase_executions || [];
  const metrics = element("div", "breakdown-metrics");
  append(metrics,
    liveBreakdownMetric(selectedRun, "Context tokens", "context_tokens", usage.context_tokens),
    liveBreakdownMetric(selectedRun, "Output tokens", "output_tokens", usage.output_tokens),
    liveBreakdownMetric(selectedRun, "Total tokens", "total_tokens", usage.total_tokens),
    toolCallMetric(selectedRun, executions, liveUsage),
    liveBreakdownMetric(selectedRun, "Reported cost", "cost", usage.cost),
    liveTotalTimeMetric(selectedRun, breakdown.started_at, terminal ? breakdown.updated_at : null));
  append(fragment, metrics);
  for (const warning of breakdown.completeness?.warnings || []) append(fragment, element("p", "notice warning", warning));
  if (executions.length) append(fragment, breakdownTable(executions, false, tableRun));
  else append(fragment, element("div", "empty-state", "No ordered phase executions are available."));
  if (breakdown.legacy_session_observations?.length) {
    append(fragment, element("h3", "", "Legacy unordered observations"), element("p", "notice warning", "These sessions cannot prove phase order or retry counts."), breakdownTable(breakdown.legacy_session_observations, true));
  }
  append(fragment, element("p", "muted mono", `Telemetry schema ${breakdown.schema_version ?? "unknown"} · ${breakdown.completeness?.complete ? "complete" : "partial"}`));
  return fragment;
}

function liveBreakdownMetric(run, label, field, value) {
  const result = metric(label, field === "cost" ? formatMoney(value) : formatNumber(value));
  if (run && ["running", "needs_decision"].includes(run.status)) {
    const node = result.querySelector(".metric-value");
    node.dataset.liveUsageRunId = run.run_id;
    node.dataset.liveUsageField = field;
    node.dataset.liveUsageFormat = "metric";
  }
  return result;
}

function liveTotalTimeMetric(run, startedAt, finishedAt = null) {
  const result = metric("Total time", runElapsed(startedAt, finishedAt));
  if (run && ["running", "needs_decision"].includes(run.status) && startedAt) {
    result.querySelector(".metric-value").dataset.liveRunStarted = String(startedAt);
  }
  return result;
}

function toolCallMetric(run, executions, liveUsage = null) {
  const completed = executions
    .filter((execution) => execution.verdict != null)
    .reduce((total, execution) => total + Math.max(0, Number(execution.tool_calls_total) || 0), 0);
  const activePhases = [...new Set(executions
    .filter((execution) => execution.verdict == null)
    .map((execution) => execution.phase)
    .filter(Boolean))];
  const active = activePhases.reduce((total, phase) => (
    total + Math.max(0, Number(liveUsage?.phase_usages?.[phase]?.tool_calls_total) || 0)
  ), 0);
  const result = metric("Total tool calls", formatNumber(completed + active));
  if (run && ["running", "needs_decision"].includes(run.status)) {
    const node = result.querySelector(".metric-value");
    node.dataset.liveUsageRunId = run.run_id;
    node.dataset.liveUsageField = "execution_tool_calls_total";
    node.dataset.liveCompletedToolCalls = String(completed);
    node.dataset.liveToolPhases = activePhases.join("\n");
  }
  return result;
}

function breakdownTable(entries, legacy = false, liveRun = null) {
  const wrap = element("div", "table-wrap");
  const table = element("table");
  if (!legacy) table.classList.add("phase-breakdown-table");
  const restartable = Boolean(!legacy && liveRun?.run_id && state.restartOptions?.eligible);
  const titles = legacy ? ["Phase", "Model", "Duration", "Context", "Output", "Total", "Tools"] : ["#", "Phase", "Type", "Model", "Verdict", "Self-check", "Duration", "Tokens", "Tools"];
  if (restartable) titles.push("Restart");
  const headRow = element("tr"); titles.forEach((title) => append(headRow, element("th", "", title)));
  const head = element("thead"); head.append(headRow);
  const body = element("tbody");
  for (const item of entries) {
    const row = element("tr");
    if (legacy) {
      append(row, element("td", "", item.phase), element("td", "mono", item.model || "—"), element("td", "mono", formatDuration(item.duration_s)), element("td", "mono", formatNumber(item.context_tokens)), element("td", "mono", formatNumber(item.output_tokens)), element("td", "mono", formatNumber(item.total_tokens)), toolCell(item));
    } else {
      const graphNode = liveRun?.phase_graph?.nodes?.find((node) => node.id === item.phase);
      const nested = Boolean(graphNode?.group);
      const duration = element("td", "mono", formatDuration(item.duration_s));
      const tokens = element("td", "mono", formatNumber(item.context_window_tokens ?? item.total_tokens));
      const liveStarted = Number(liveRun?.active_phases?.[item.phase]);
      const isLive = Boolean(liveRun?.run_id && item.verdict == null && liveStarted);
      const verdict = element("td");
      verdict.append(badge(item.verdict || (isLive ? "active" : "incomplete")));
      const selfCheck = element("td");
      const configuredSelfCheck = graphNode?.self_check === true;
      const selfCheckStatus = configuredSelfCheck ? "enabled" : "disabled";
      const selfCheckBadge = badge(selfCheckStatus);
      selfCheck.append(selfCheckBadge);
      if (isLive) {
        row.classList.add("phase-row-active");
        row.dataset.livePhaseRunId = liveRun.run_id;
        row.dataset.livePhaseId = item.phase;
        row.dataset.livePhasePriorTokens = String(entries
          .filter((prior) => prior.phase === item.phase && prior.sequence < item.sequence)
          .reduce((total, prior) => total + Math.max(0, Number(prior.context_window_tokens ?? prior.total_tokens) || 0), 0));
        duration.dataset.livePhaseStarted = String(liveStarted);
        tokens.dataset.livePhaseTokens = "true";
      }
      if (nested) row.classList.add("phase-row-nested");
      const phaseLabel = element("td", "phase-row-label", item.label || item.phase);
      if (nested) phaseLabel.prepend(element("span", "phase-row-branch", "↳"));
      const model = element("td", "mono phase-row-model", item.model || "—");
      duration.classList.add("phase-row-duration");
      append(row, element("td", "mono", item.sequence), phaseLabel, element("td", "", item.phase_type || "—"), model, verdict, selfCheck, duration, tokens, toolCell(item, Boolean(isLive)));
      if (restartable) row.append(restartPhaseCell(liveRun.run_id, item));
      if (item.rejection_reason) {
        const reasonRow = element("tr"); const cell = element("td", "phase-row-reason"); cell.colSpan = restartable ? 10 : 9; cell.append(diagnostic(item.rejection_reason, "notice danger")); reasonRow.append(cell); body.append(row, reasonRow); continue;
      }
    }
    body.append(row);
  }
  append(table, head, body); wrap.append(table); return wrap;
}

function restartPhaseCell(runId, execution) {
  const cell = element("td", "phase-row-restart");
  const sequence = Number(execution.sequence);
  const choice = (state.restartOptions?.phases || []).find((item) => (
    item.id === execution.phase && Number(item.sequence) === sequence
  ));
  if (!choice) {
    cell.append(element("span", "muted", "—"));
    return cell;
  }
  const action = button("Apply", "secondary", async () => {
    const label = choice.label || choice.id;
    if (!window.confirm(`Restart from ${label} execution #${choice.call_number}?`)) return;
    action.disabled = true;
    const result = await mutation(
      () => QuillApi.restart(runId, choice.id, choice.sequence),
      `Restarting from ${label}`,
    );
    if (result?.run_id) location.hash = `#/runs/${encodeURIComponent(result.run_id)}`;
    else action.disabled = false;
  });
  cell.append(action);
  return cell;
}

function toolCell(item, live = false) {
  const cell = element("td", "phase-row-tools");
  const details = element("details");
  const summary = element("summary", "mono", formatNumber(item.tool_calls_total || 0));
  if (live) summary.dataset.livePhaseToolsSummary = "true";
  append(details, summary);
  const list = element("div", "mono muted");
  if (live) list.dataset.livePhaseToolsList = "true";
  renderToolList(list, item.tool_calls_by_name || {});
  append(details, list); cell.append(details); return cell;
}

function renderToolList(node, tools) {
  node.replaceChildren();
  for (const [name, count] of Object.entries(tools)) append(node, element("div", "", `${name}: ${count}`));
  if (!Object.keys(tools).length) append(node, element("div", "", "No tool calls"));
}

function renderArtifacts() {
  const fragment = document.createDocumentFragment();
  if (!state.artifacts.length) {
    append(fragment, element("div", "empty-state", "This run has no readable artifacts."));
    return fragment;
  }
  const controls = element("div", "button-row artifact-actions");
  const downloadAll = element("a", "button", "Download all");
  downloadAll.href = QuillApi.artifactsArchiveUrl(state.runDetail.run_id);
  downloadAll.download = `${state.runDetail.run_id}-artifacts.zip`;
  controls.append(downloadAll);
  const wrap = element("div", "table-wrap");
  const table = element("table");
  const head = element("tr");
  append(head, element("th", "", "Artifact"), element("th", "", "Size"), element("th", "", ""));
  const thead = element("thead"); thead.append(head);
  const body = element("tbody");
  for (const artifact of state.artifacts) {
    const row = element("tr");
    const action = element("td", "artifact-download-cell");
    const download = element("a", "button secondary small", "Download");
    download.href = QuillApi.artifactDownloadUrl(state.runDetail.run_id, artifact.name);
    download.download = artifact.name;
    action.append(download);
    append(row, element("td", "mono", artifact.name), element("td", "mono", formatBytes(artifact.size)), action);
    body.append(row);
  }
  append(table, thead, body); wrap.append(table);
  append(fragment, controls, wrap);
  return fragment;
}

function branchMarker(branch) {
  const marks = [];
  if (branch.current) marks.push("current");
  if (branch.local && branch.remote) marks.push("local+remote");
  else if (branch.local) marks.push("local only");
  else if (branch.remote) marks.push("remote only");
  return marks.join(" · ") || "unknown";
}

function workspaceSummary(selected) {
  return detailGrid([
    ["Workspace", state.workspaces.repo || "—"],
    ["Checked out", state.workspaces.current || "—"],
    ["Selected branch", selected ? selected.name : "—"],
    ["Availability", selected ? branchMarker(selected) : "—"],
  ]);
}

function renderWorkspaces() {
  const fragment = document.createDocumentFragment();
  append(
    fragment,
    heading(
      "SERVER CHECKOUTS",
      "Workspaces",
      "Administer the persistent per-repo clones this server runs pipelines in. Fetch and fast-forward a branch, or delete a stale local branch — origin is never touched.",
    ),
  );
  if (state.errors.workspaces) append(fragment, element("p", "notice danger", state.errors.workspaces));

  const control = panel("Checkout", "workspace-panel");
  if (loading("workspaces") && !state.workspaces.list.length) {
    append(control, element("div", "loading-bar"));
  }
  if (!state.workspaces.list.length && !loading("workspaces")) {
    append(
      control,
      element("div", "empty-state", "No repositories have been cloned on this server yet. A workspace appears here after its first run prepares it."),
    );
    append(fragment, control);
    return fragment;
  }

  const form = element("div", "workspace-form");
  const repoField = choiceField(
    "Workspace",
    state.workspaces.list.map((item) => ({ value: item.repo, label: `${item.repo} · on ${item.branch}` })),
    state.workspaces.repo,
    "No workspaces",
  );
  repoField.input.addEventListener("change", async () => {
    state.workspaces.repo = repoField.input.value;
    state.workspaces.branch = "";
    state.workspaces.result = null;
    await refreshWorkspaceBranches(state.workspaces.repo);
  });

  const branches = state.workspaces.branches;
  const branchLoading = loading("workspace-branches");
  const branchField = choiceField(
    "Branch",
    branches.map((item) => ({ value: item.name, label: `${item.name} · ${branchMarker(item)}` })),
    state.workspaces.branch,
    branchLoading ? "Loading branches…" : "No branches on this workspace",
  );
  branchField.input.addEventListener("change", () => {
    state.workspaces.branch = branchField.input.value;
    state.workspaces.result = null;
    render();
  });
  append(form, repoField.label, branchField.label);
  append(control, form);

  const selected = branches.find((item) => item.name === state.workspaces.branch) || null;
  append(control, workspaceSummary(selected));

  const mutating = loading("workspace-mutation");
  const actions = element("div", "button-row");
  const pull = button(mutating ? "Working…" : "Fetch & pull", "", () => pullSelectedBranch(selected));
  pull.disabled = mutating || !selected || !selected.remote;
  const remove = button("Delete local branch", "danger", () => deleteSelectedBranch(selected));
  remove.disabled = mutating || !selected || !selected.local;
  append(actions, pull, remove);
  append(control, actions);
  if (selected && !selected.remote) {
    append(control, element("p", "notice", "This branch exists only locally, so there is nothing on origin to pull."));
  }
  if (state.workspaces.result) append(control, element("p", "notice", state.workspaces.result));
  append(fragment, control);
  return fragment;
}

async function pullSelectedBranch(selected) {
  if (!selected || !selected.remote) return;
  const repo = state.workspaces.repo;
  setLoading("workspace-mutation", true);
  state.workspaces.result = null;
  render();
  const result = await mutation(() => QuillApi.pullWorkspaceBranch(repo, selected.name), null);
  setLoading("workspace-mutation", false);
  if (!result) return render();
  state.workspaces.result = result.message;
  state.workspaces.branch = result.branch;
  toast(result.message, "success");
  // Reload both dropdowns so current/local/remote flags move together after the checkout changes.
  await refreshWorkspaces();
}

async function deleteSelectedBranch(selected) {
  if (!selected || !selected.local) return;
  const repo = state.workspaces.repo;
  const name = selected.name;
  const message = selected.current
    ? `Delete the local branch ${name}? origin is NOT deleted. Because it is the current checkout, the workspace first switches to its default branch.`
    : `Delete the local branch ${name}? origin is NOT deleted and the remote branch stays selectable.`;
  if (!(await confirmAction(`Delete local ${name}`, message))) return;
  setLoading("workspace-mutation", true);
  state.workspaces.result = null;
  render();
  const result = await mutation(() => QuillApi.deleteWorkspaceBranch(repo, name), null);
  setLoading("workspace-mutation", false);
  if (!result) return render();
  state.workspaces.result = result.message;
  state.workspaces.branch = result.branch;
  toast(result.message, "success");
  await refreshWorkspaces();
}

function renderCatalog(kind) {
  const isPersona = kind === "personas";
  const entries = isPersona ? state.personas : state.skills;
  const selected = isPersona ? state.persona : state.skill;
  const creating = isPersona ? state.personaCreating : state.skillCreating;
  const fragment = document.createDocumentFragment();
  const newButton = button(`New ${isPersona ? "persona" : "skill"}`, "", () => beginCatalogCreate(kind));
  append(fragment, heading("SHARED LIBRARY", isPersona ? "Personas" : "Skills", `Editing the server library at ${isPersona ? state.personaRoot || "…" : state.skillRoot || "…"}. Every change requires an audit reason.`, newButton));
  const layout = element("div", "catalog-layout");
  const sidebar = panel(null);
  const search = field(`Search ${kind}`, "search", state.catalogSearch, "Filter by name or description");
  search.input.addEventListener("input", () => { state.catalogSearch = search.input.value; render(); });
  sidebar.append(search.label);
  const list = element("div", "catalog-list");
  const query = state.catalogSearch.toLowerCase();
  for (const entry of entries.filter((item) => `${item.name} ${item.description}`.toLowerCase().includes(query))) {
    const item = element("button", "catalog-item"); item.type = "button";
    item.setAttribute("aria-current", String(state.route.id === entry.name && !creating));
    append(item, element("strong", "mono", entry.name), element("small", "", entry.description || "No description"));
    item.addEventListener("click", () => navigateCatalog(kind, entry.name)); list.append(item);
  }
  if (!list.children.length) append(list, element("div", "empty-state", "No matching entries."));
  append(sidebar, list);
  const editor = panel(null);
  if (loading(kind) && !selected && !creating) append(editor, element("div", "loading-bar"), element("div", "empty-state", "Loading library…"));
  else if (state.errors[kind]) append(editor, element("div", "error-state", state.errors[kind]));
  else if (selected || creating) append(editor, renderCatalogEditor(kind));
  else append(editor, element("div", "empty-state", `Select a ${isPersona ? "persona" : "skill"} to inspect it, or create a new one.`));
  append(layout, sidebar, editor); append(fragment, layout); return fragment;
}

function beginCatalogCreate(kind) {
  if (!discardEditor()) return;
  state.editorDirty = false;
  if (kind === "personas") { state.personaCreating = true; state.persona = { name: "", body: "", description: "", suits: null }; }
  else { state.skillCreating = true; state.skill = { name: "", body: "", description: "", files: [] }; }
  history.replaceState(null, "", `#/${kind}`); state.route = parseRoute(location.hash); render();
}

function navigateCatalog(kind, name) {
  if (!discardEditor()) return;
  state.editorDirty = false;
  state.personaCreating = false; state.skillCreating = false; state.skillFile = null; state.skillFileCreating = false;
  location.hash = `#/${kind}/${encodeURIComponent(name)}`;
}

function renderCatalogEditor(kind) {
  const isPersona = kind === "personas";
  const detail = isPersona ? state.persona : state.skill;
  const creating = isPersona ? state.personaCreating : state.skillCreating;
  const fragment = document.createDocumentFragment();
  const toolbar = element("div", "editor-toolbar");
  const title = element("div", "editor-title");
  append(title, element("p", "eyebrow", creating ? "NEW ENTRY" : "EDITING"), element("h2", "mono", detail.name || `new-${isPersona ? "persona" : "skill"}`));
  const dirty = element("span", "unsaved", state.editorDirty ? "● UNSAVED" : "SYNCHRONIZED"); dirty.id = "dirty-indicator";
  append(toolbar, title, dirty); append(fragment, toolbar);
  const name = field("Name", "text", detail.name || "", isPersona ? "review-plan" : "my-skill"); name.input.disabled = !creating;
  const bodyWrap = element("label"); const body = element("textarea"); body.value = detail.body || "";
  append(bodyWrap, element("span", "", isPersona ? "Persona Markdown (frontmatter included)" : "SKILL.md (frontmatter included)"), body);
  const reason = field("Audit reason", "text", "", "Describe why this change is being made");
  for (const input of [name.input, body, reason.input]) input.addEventListener("input", markDirty);
  append(fragment, name.label, bodyWrap, reason.label);
  const validation = element("div", "field-error"); append(fragment, validation);
  const actions = element("div", "button-row");
  const save = button(creating ? "Create" : "Save changes", "", async () => {
    validation.textContent = "";
    if (!validCatalogName(name.input.value)) { validation.textContent = "Use a safe 1–100 character catalog name."; return; }
    if (!body.value.trim()) { validation.textContent = "The Markdown body cannot be empty."; return; }
    if (!validReason(reason.input.value)) { validation.textContent = "Audit reason must be 3–200 characters."; return; }
    save.disabled = true;
    const operation = isPersona
      ? creating ? () => QuillApi.createPersona(name.input.value.trim(), body.value, reason.input.value.trim()) : () => QuillApi.updatePersona(detail.name, body.value, reason.input.value.trim())
      : creating ? () => QuillApi.createSkill(name.input.value.trim(), body.value, reason.input.value.trim()) : () => QuillApi.updateSkill(detail.name, body.value, reason.input.value.trim());
    const result = await mutation(operation);
    if (result) {
      state.editorDirty = false; state.personaCreating = false; state.skillCreating = false;
      showWriteResult(result);
      location.hash = `#/${kind}/${encodeURIComponent(result.name)}`;
      await refreshCatalog(kind, result.name, { quiet: true });
    } else {
      await refreshCatalog(kind, detail.name, { quiet: true });
    }
  });
  actions.append(save);
  if (!creating) actions.append(button(`Delete ${isPersona ? "persona" : "skill"}`, "danger", () => deleteCatalogEntry(kind, detail.name)));
  append(fragment, actions);
  if (!isPersona && !creating) append(fragment, renderSkillFiles(detail));
  return fragment;
}

function markDirty() {
  state.editorDirty = true;
  const indicator = document.querySelector("#dirty-indicator");
  if (indicator) indicator.textContent = "● UNSAVED";
}

function discardEditor() {
  return !state.editorDirty || window.confirm("Discard unsaved catalog edits?");
}

async function deleteCatalogEntry(kind, name) {
  const reason = window.prompt(`Audit reason for deleting ${name}:`, "remove obsolete entry");
  if (reason === null) return;
  if (!validReason(reason)) return toast("Audit reason must be 3–200 characters", "warning");
  if (!(await confirmAction(`Delete ${name}`, `This removes the entire ${kind === "personas" ? "persona" : "skill"}. Type ${name} to confirm.`, name))) return;
  const result = await mutation(() => kind === "personas" ? QuillApi.deletePersona(name, reason.trim()) : QuillApi.deleteSkill(name, reason.trim()));
  if (!result) {
    await refreshCatalog(kind, name, { quiet: true });
    return;
  }
  showWriteResult(result);
  state.editorDirty = false; state.persona = null; state.skill = null;
  location.hash = `#/${kind}`;
  await refreshCatalog(kind, null, { quiet: true });
}

function renderSkillFiles(skill) {
  const section = element("section");
  const header = element("div", "panel-header");
  append(header, element("h2", "", `Auxiliary files (${skill.files?.length || 0})`), button("New file", "secondary small", () => { state.skillFileCreating = true; state.skillFile = { path: "", content: "" }; render(); }));
  append(section, header);
  const layout = element("div", "file-layout");
  const list = element("div", "catalog-list");
  for (const path of skill.files || []) {
    const item = element("button", "catalog-item mono", path); item.type = "button";
    item.setAttribute("aria-current", String(state.skillFile?.path === path && !state.skillFileCreating));
    item.addEventListener("click", () => loadSkillFile(skill.name, path)); list.append(item);
  }
  if (!list.children.length) append(list, element("div", "empty-state", "No auxiliary files."));
  const editor = element("div");
  if (state.skillFile) append(editor, renderSkillFileEditor(skill));
  else append(editor, element("div", "empty-state", "Select a file to load its content."));
  append(layout, list, editor); append(section, layout); return section;
}

async function loadSkillFile(skillName, path) {
  if (!discardEditor()) return;
  try {
    const file = await QuillApi.skillFile(skillName, path);
    state.skillFile = { path, content: file.content }; state.skillFileCreating = false; state.editorDirty = false; render();
  } catch (error) { handleError(error, "skill-file"); }
}

function renderSkillFileEditor(skill) {
  const file = state.skillFile;
  const creating = state.skillFileCreating;
  const fragment = document.createDocumentFragment();
  const path = field("Relative path", "text", file.path, "references/guide.md"); path.input.disabled = !creating;
  const contentWrap = element("label"); const content = element("textarea"); content.value = file.content || "";
  append(contentWrap, element("span", "", "File content"), content);
  const reason = field("Audit reason", "text", "", "Explain this file change");
  [path.input, content, reason.input].forEach((input) => input.addEventListener("input", markDirty));
  const error = element("div", "field-error");
  const actions = element("div", "button-row");
  append(actions, button(creating ? "Create file" : "Save file", "", async () => {
    error.textContent = "";
    if (!path.input.value.trim() || !path.input.value.split("/").every((part) => part && part !== "." && part !== "..")) { error.textContent = "Enter a safe relative file path."; return; }
    if (!validReason(reason.input.value)) { error.textContent = "Audit reason must be 3–200 characters."; return; }
    const result = await mutation(() => QuillApi.writeSkillFile(skill.name, path.input.value.trim(), content.value, reason.input.value.trim()));
    if (result) {
      state.editorDirty = false; showWriteResult(result);
      await refreshCatalog("skills", skill.name, { quiet: true });
      await loadSkillFile(skill.name, path.input.value.trim());
    } else {
      await refreshCatalog("skills", skill.name, { quiet: true });
    }
  }));
  if (!creating) actions.append(button("Delete file", "danger", async () => {
    const audit = window.prompt(`Audit reason for deleting ${file.path}:`, "remove obsolete file");
    if (audit === null) return;
    if (!validReason(audit)) return toast("Audit reason must be 3–200 characters", "warning");
    if (!(await confirmAction("Delete auxiliary file", `Delete ${file.path}?`))) return;
    const result = await mutation(() => QuillApi.deleteSkillFile(skill.name, file.path, audit.trim()));
    if (!result) {
      await refreshCatalog("skills", skill.name, { quiet: true });
      return;
    }
    showWriteResult(result);
    state.skillFile = null; state.skillFileCreating = false; state.editorDirty = false;
    await refreshCatalog("skills", skill.name, { quiet: true });
  }));
  append(fragment, path.label, contentWrap, reason.label, error, actions); return fragment;
}

function showWriteResult(result) {
  if (result.committed && result.pushed) toast(`Committed and pushed ${result.sha || result.name}`, "success");
  else if (result.committed) toast(`Committed ${result.sha || result.name}, but not pushed: ${result.error || "remote unavailable"}`, "warning", 9000);
  else toast(result.error || "No catalog change was committed", "neutral");
}

function visibleMemories() {
  return state.memoryRepo
    ? state.memories.filter((memory) => memory.repo === state.memoryRepo)
    : state.memories;
}

async function removeMemories(memoryIds, deleteAll = false) {
  if (!deleteAll && !memoryIds.length) return;
  const count = deleteAll ? state.memories.length : memoryIds.length;
  const message = deleteAll
    ? "Delete every verified memory and every unresolved blocker archive across all repositories? This cannot be undone."
    : `Delete ${count} selected memor${count === 1 ? "y" : "ies"}? This removes every occurrence behind the selected row${count === 1 ? "" : "s"}.`;
  if (!await confirmAction(deleteAll ? "Delete all memories" : "Delete selected memories", message)) return;
  setLoading("delete-memories", true);
  render();
  try {
    const result = await QuillApi.deleteMemories(memoryIds, deleteAll);
    state.selectedMemoryIds.clear();
    toast(
      deleteAll
        ? "All memory history was cleared."
        : `${result.deleted.length} memor${result.deleted.length === 1 ? "y" : "ies"} deleted.`,
      "success",
    );
    await refreshMemories({ quiet: true });
  } catch (error) {
    handleError(error, "memories");
  } finally {
    setLoading("delete-memories", false);
    render();
  }
}

function memoriesTable(memories) {
  const wrap = element("div", "table-wrap memory-table");
  const table = element("table");
  const head = element("thead");
  const headRow = element("tr");
  const selectAllCell = element("th", "run-select-cell");
  const selectAll = element("input");
  selectAll.type = "checkbox";
  selectAll.setAttribute("aria-label", "Select all visible memories");
  selectAll.checked = memories.length > 0 && memories.every((memory) => state.selectedMemoryIds.has(memory.memory_id));
  selectAll.indeterminate = !selectAll.checked && memories.some((memory) => state.selectedMemoryIds.has(memory.memory_id));
  selectAll.disabled = !memories.length;
  selectAll.addEventListener("change", () => {
    for (const memory of memories) {
      if (selectAll.checked) state.selectedMemoryIds.add(memory.memory_id);
      else state.selectedMemoryIds.delete(memory.memory_id);
    }
    render();
  });
  selectAllCell.append(selectAll);
  headRow.append(selectAllCell);
  for (const title of ["Repository", "Finding", "Phases", "Occurrences", "Last verified", "Changed files"]) {
    headRow.append(element("th", "", title));
  }
  head.append(headRow);
  const body = element("tbody");
  for (const memory of memories) {
    const row = element("tr");
    const selectCell = element("td", "run-select-cell");
    const selectMemory = element("input");
    selectMemory.type = "checkbox";
    selectMemory.checked = state.selectedMemoryIds.has(memory.memory_id);
    selectMemory.setAttribute("aria-label", `Select memory from ${memory.repo}`);
    selectMemory.addEventListener("change", () => {
      if (selectMemory.checked) state.selectedMemoryIds.add(memory.memory_id);
      else state.selectedMemoryIds.delete(memory.memory_id);
      render();
    });
    selectCell.append(selectMemory);
    const verified = formatTime(Date.parse(memory.last_verified_at || "") / 1000);
    const files = memory.changed_files || [];
    const filesCell = element("td");
    if (files.length) {
      const details = element("details", "memory-files");
      append(details, element("summary", "", `${files.length} file${files.length === 1 ? "" : "s"}`));
      for (const path of files) append(details, element("div", "mono", path));
      filesCell.append(details);
    } else {
      filesCell.textContent = "—";
    }
    append(
      row,
      selectCell,
      element("td", "mono", memory.repo),
      element("td", "memory-finding", memory.finding),
      element("td", "mono", (memory.phases || []).join(", ") || "—"),
      element("td", "mono", formatNumber(memory.occurrences)),
      element("td", "", verified.label),
      filesCell,
    );
    body.append(row);
  }
  append(table, head, body);
  wrap.append(table);
  return wrap;
}

function renderMemories() {
  const fragment = document.createDocumentFragment();
  append(
    fragment,
    heading(
      "REPOSITORY MEMORY",
      "Memories",
      "Inspect and remove verified blocker lessons that Quill can inject into eligible reasoning phases.",
    ),
  );
  if (state.errors.memories) append(fragment, element("p", "notice danger", state.errors.memories));
  const repositories = [...new Set(state.memories.map((memory) => memory.repo))].sort();
  const controls = panel(null, "memory-controls");
  const filter = selectField("Repository", ["", ...repositories], state.memoryRepo);
  filter.input.addEventListener("change", () => {
    state.memoryRepo = filter.input.value;
    render();
  });
  const count = state.selectedMemoryIds.size;
  const actions = element("div", "button-row");
  const removeSelected = button(
    loading("delete-memories") ? "Deleting…" : `Delete selected${count ? ` (${count})` : ""}`,
    "danger small",
    () => removeMemories([...state.selectedMemoryIds]),
  );
  removeSelected.disabled = !count || loading("delete-memories");
  const removeAll = button("Delete all", "danger small", () => removeMemories([], true));
  removeAll.disabled = !state.memoryArchivedEvents || loading("delete-memories");
  append(actions, removeSelected, removeAll);
  const controlCopy = element("div");
  append(
    controlCopy,
    filter.label,
    element(
      "p",
      "muted memory-count",
      `${state.memories.length} verified · ${state.memoryArchivedEvents} archived event${state.memoryArchivedEvents === 1 ? "" : "s"}`,
    ),
  );
  append(controls, controlCopy, actions);
  append(fragment, controls);
  const memories = visibleMemories();
  if (loading("memories") && !state.memories.length) {
    append(fragment, element("div", "loading-bar"));
  } else if (!memories.length) {
    append(
      fragment,
      element(
        "div",
        "empty-state panel",
        state.memories.length
          ? "No memories match this repository."
          : "No verified memories yet. Quill will list lessons here after an enabled gate resolves a blocker.",
      ),
    );
  } else {
    append(fragment, memoriesTable(memories));
  }
  return fragment;
}

function renderApi() {
  const fragment = document.createDocumentFragment();
  const links = element("div", "button-row");
  for (const [href, label] of [["/docs", "Swagger UI"], ["/openapi.json", "OpenAPI JSON"]]) {
    const link = element("a", "button secondary", label); link.href = href; link.target = "_blank"; link.rel = "noopener"; links.append(link);
  }
  append(fragment, heading("DISCOVERY", "API", "Live discovery data from this exact Quill deployment, including browser run submission.", links));
  const system = element("div", "grid three");
  append(system, metric("Quill version", state.version?.quill || "—"), metric("API version", state.version?.api || "—"), metric("Config filename", state.init?.config_filename || "—"));
  append(fragment, system);
  const endpoints = panel("Advertised Endpoints");
  const details = element("dl", "detail-list");
  for (const [name, endpoint] of Object.entries(state.init?.endpoints || {})) {
    const item = element("div", "detail"); append(item, element("dt", "", name), element("dd", "mono", endpoint)); details.append(item);
  }
  append(endpoints, details); append(fragment, endpoints);
  const config = panel("Starter Configuration");
  const actions = element("div", "button-row"); append(actions, button("Copy config", "secondary", () => copyText(state.init?.starter_config || "")));
  append(config, actions, element("pre", "code-block", state.init?.starter_config || "No starter configuration returned."));
  append(fragment, config);
  return fragment;
}

// -- models -------------------------------------------------------------------

// Model-operation state rides on every telemetry sample. Mutate the row actions instead of
// rebuilding the table at 8 Hz, then refresh residency once the operation settles.
function updateModelSwitchRegions() {
  const switchState = state.telemetry?.model_switch || state.models?.switch || {};
  const busy = ["switching", "unloading"].includes(switchState.status);
  for (const control of document.querySelectorAll("[data-model-action]")) {
    const action = control.dataset.modelAction;
    const target = control.dataset.modelId === switchState.model_id;
    control.disabled = busy;
    control.textContent = busy && target
      ? action === "load" ? "Loading…" : "Unloading…"
      : action;
  }
  // A finished load or unload changes residency; refresh once on the transition.
  const lastBusy = ["switching", "unloading"].includes(lastModelOperationStatus);
  if (!busy && lastBusy) {
    if (switchState.status === "failed") {
      toast(switchState.error || "Model operation failed", "danger");
    } else {
      toast(switchState.status === "unloaded" ? "Model unloaded" : "Model loaded", "success");
    }
    refreshModels({ quiet: true });
  }
  lastModelOperationStatus = switchState.status || "idle";
}

async function refreshModels({ quiet = false, probe = false } = {}) {
  try {
    state.models = probe ? await QuillApi.refreshModels() : await QuillApi.models();
    if (state.route.section === "models") render();
  } catch (error) { handleError(error, "models", !quiet); }
}

async function loadModel(modelId, { force = false } = {}) {
  try {
    const result = await QuillApi.switchModel(modelId, force);
    if (state.models) state.models.switch = result;
    updateModelSwitchRegions();
    toast(`Loading ${modelId}…`, "success");
  } catch (error) {
    const detail = error?.payload?.detail;
    if (error?.status === 409 && detail && typeof detail === "object" && detail.runs) {
      const runs = detail.runs.join(", ");
      if (window.confirm(
        `${detail.message}\n\nActive runs: ${runs}\n\nStarting this model stops the one they are using. Continue?`,
      )) await loadModel(modelId, { force: true });
      return;
    }
    handleError(error, "models", true);
  }
}

async function unloadModel(modelId, { force = false } = {}) {
  try {
    const result = await QuillApi.unloadModel(modelId, force);
    if (state.models) state.models.switch = result;
    updateModelSwitchRegions();
    toast(`Unloading ${modelId}…`, "success");
  } catch (error) {
    const detail = error?.payload?.detail;
    if (error?.status === 409 && detail && typeof detail === "object" && detail.runs) {
      const runs = detail.runs.join(", ");
      if (window.confirm(
        `${detail.message}\n\nActive runs: ${runs}\n\nUnloading this model will interrupt them. Continue?`,
      )) await unloadModel(modelId, { force: true });
      return;
    }
    handleError(error, "models", true);
  }
}

function renderModels() {
  const fragment = document.createDocumentFragment();
  const refresh = button("Re-scan services", "secondary", () => refreshModels({ probe: true }));
  append(fragment, heading(
    "MODEL SERVER",
    "Models",
    "Every vLLM model this machine can serve. Load or unload its discovered systemd service directly from the table.",
    refresh,
  ));

  const info = state.models || {};
  const switchable = info.switchable || [];
  const residentIds = info.loaded || [];
  const residentEntry = switchable.find((entry) => entry.resident);
  // -- resident ---------------------------------------------------------------
  const statusPanel = panel("Resident");
  const summary = element("div", "resident-summary");
  const identity = element("div", "resident-badges");
  const modelBadge = element("span", "badge resident-model", residentIds.join(", ") || "Nothing loaded");
  modelBadge.dataset.tone = residentEntry ? "active" : "danger";
  append(identity, modelBadge);
  if (residentEntry?.max_model_len) {
    const context = element("span", "badge", `${formatNumber(residentEntry.max_model_len)} ctx`);
    context.dataset.tone = "success";
    append(identity, context);
  }
  append(
    summary,
    identity,
    element("p", "muted resident-endpoint", info.reachable ? `Ready at ${info.url || "vllm"}` : "Server unreachable"),
  );
  append(statusPanel, summary);
  append(statusPanel, renderVllmThroughput());
  append(fragment, statusPanel);

  // -- discovered -------------------------------------------------------------
  const listPanel = panel("Discovered services");
  if (!switchable.length) {
    append(listPanel, element("div", "empty-state", "No vLLM model services were discovered on this machine."));
    append(fragment, listPanel);
    return fragment;
  }
  const wrap = element("div", "table-wrap");
  const table = element("table");
  const headRow = element("tr");
  for (const column of ["Model", "Service", "Context", "Concurrent", "Batch", "TP", "Quant", "KV", "Action"]) {
    append(headRow, element("th", "", column));
  }
  const head = element("thead"); head.append(headRow);
  const body = element("tbody");
  for (const entry of switchable) {
    const row = element("tr");
    const action = element("td", "model-action-cell");
    let control = null;
    if (entry.resident) {
      control = button("Unload", "danger small", () => unloadModel(entry.model_id));
      control.dataset.modelAction = "unload";
    } else if (entry.available) {
      control = button("Load", "success small", () => loadModel(entry.model_id));
      control.dataset.modelAction = "load";
    }
    if (control) {
      control.dataset.modelId = entry.model_id;
      append(action, control);
    } else {
      const unavailable = element("span", "muted", "Unavailable");
      unavailable.title = entry.unavailable_reason || "This service cannot be controlled";
      append(action, unavailable);
    }
    append(
      row,
      element("td", "", entry.model_id),
      element("td", "muted", entry.service),
      element("td", "", entry.max_model_len ? formatNumber(entry.max_model_len) : "—"),
      element("td", "", entry.max_concurrency ?? "—"),
      element("td", "", entry.max_batched_tokens ? formatNumber(entry.max_batched_tokens) : "—"),
      element("td", "", entry.tensor_parallel_size ?? "—"),
      element("td", "muted", entry.quantization || "—"),
      element("td", "muted", entry.kv_cache_dtype || "—"),
      action,
    );
    body.append(row);
  }
  append(table, head, body);
  wrap.append(table);
  append(listPanel, wrap);
  append(fragment, listPanel);
  return fragment;
}

function renderSettings() {
  const fragment = document.createDocumentFragment();
  append(fragment, heading(
    "DISPLAY SETTINGS",
    "Settings",
    "Configure how live hardware telemetry is scaled. Raw measurements are not changed.",
  ));
  const settingsPanel = panel("Temperature Ranges");
  const form = element("form", "settings-form");
  const fields = [
    ["CPU minimum (°C)", "cpu_temperature_min_c"],
    ["CPU maximum (°C)", "cpu_temperature_max_c"],
    ["GPU minimum (°C)", "gpu_temperature_min_c"],
    ["GPU maximum (°C)", "gpu_temperature_max_c"],
  ].map(([label, key]) => {
    const control = field(label, "number", state.telemetrySettings?.[key]);
    control.input.min = "-20";
    control.input.max = "150";
    control.input.step = "1";
    control.input.name = key;
    return control;
  });
  const save = button("Save settings", "primary");
  save.type = "submit";
  append(form, ...fields.map((item) => item.label), save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const next = Object.fromEntries(fields.map((item) => [item.input.name, Number(item.input.value)]));
    save.disabled = true;
    const result = await mutation(() => QuillApi.updateTelemetrySettings(next), "Telemetry settings saved");
    save.disabled = false;
    if (result) {
      state.telemetrySettings = result;
      render();
      updateTelemetryGauges();
    }
  });
  append(
    settingsPanel,
    element("p", "muted", "Temperatures at or below the minimum show an empty bar. Temperatures at or above the maximum fill the bar."),
    form,
  );
  append(fragment, settingsPanel);
  return fragment;
}

async function mutation(operation, successMessage = null) {
  try {
    const result = await operation();
    if (successMessage) toast(successMessage, "success");
    return result;
  } catch (error) {
    handleError(error, "mutation");
    return null;
  }
}

function confirmAction(title, message, required = null) {
  const dialog = document.querySelector("#confirm-dialog");
  const inputWrap = document.querySelector("#confirm-input-wrap");
  const input = document.querySelector("#confirm-input");
  const submit = document.querySelector("#confirm-submit");
  document.querySelector("#confirm-title").textContent = title;
  document.querySelector("#confirm-message").textContent = message;
  inputWrap.hidden = !required;
  input.value = "";
  submit.disabled = Boolean(required);
  const handler = () => { submit.disabled = Boolean(required) && input.value !== required; };
  input.addEventListener("input", handler);
  dialog.showModal();
  if (required) input.focus();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      input.removeEventListener("input", handler);
      resolve(dialog.returnValue === "confirm");
    }, { once: true });
  });
}

async function copyText(value) {
  try { await navigator.clipboard.writeText(String(value)); toast("Copied to clipboard", "success"); }
  catch { toast("Clipboard access was denied", "warning"); }
}

function downloadText(name, value) {
  const blob = new Blob([String(value)], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  URL.revokeObjectURL(url);
}

function render() {
  state.route = parseRoute(location.hash);
  document.querySelectorAll(".primary-nav a").forEach((link) => link.setAttribute("aria-current", link.dataset.route === state.route.section ? "page" : "false"));
  main.replaceChildren();
  if (state.route.section === "runs") main.append(renderRuns());
  else if (state.route.section === "queue") main.append(renderQueue());
  else if (state.route.section === "workspaces") main.append(renderWorkspaces());
  else if (state.route.section === "memories") main.append(renderMemories());
  else if (state.route.section === "personas") main.append(renderCatalog("personas"));
  else if (state.route.section === "skills") main.append(renderCatalog("skills"));
  else if (state.route.section === "models") main.append(renderModels());
  else if (state.route.section === "settings") main.append(renderSettings());
  else if (state.route.section === "api") main.append(renderApi());
  else main.append(renderOverview());
  updateElapsed();
}

async function handleRoute() {
  state.route = parseRoute(location.hash);
  state.errors = {};
  render();
  if (["overview", "runs"].includes(state.route.section)) {
    const refreshes = [refreshRuns({ quiet: true })];
    if (state.route.section === "overview") refreshes.push(refreshProjectQueue({ quiet: true }));
    if (state.route.section === "runs" && !state.github.repositories.length) {
      refreshes.push(refreshGitHubRepositories({ quiet: true }));
    }
    await Promise.all(refreshes);
  }
  else if (state.route.section === "queue") await refreshQueuePage({ quiet: true });
  else if (state.route.section === "workspaces") await refreshWorkspaces({ quiet: true });
  else if (state.route.section === "memories") await refreshMemories({ quiet: true });
  else if (state.route.section === "personas") await refreshCatalog("personas", state.route.id, { quiet: true });
  else if (state.route.section === "skills") await refreshCatalog("skills", state.route.id, { quiet: true });
  else if (state.route.section === "models") await refreshModels({ quiet: true });
  else if (state.route.section === "settings") await refreshSystem({ quiet: true });
  else if (state.route.section === "api" && !state.init) await refreshSystem({ quiet: true });
}

function updateElapsed() {
  document.querySelectorAll(".elapsed[data-started]").forEach((node) => {
    node.textContent = `elapsed ${runElapsed(Number(node.dataset.started))}`;
  });
  document.querySelectorAll("[data-live-phase-started]").forEach((node) => {
    const elapsed = Date.now() / 1000 - Number(node.dataset.livePhaseStarted);
    node.textContent = formatDuration(Number(node.dataset.livePhaseBase || 0) + elapsed);
  });
  document.querySelectorAll("[data-live-run-started]").forEach((node) => {
    node.textContent = runElapsed(Number(node.dataset.liveRunStarted));
  });
}

window.addEventListener("hashchange", () => {
  if (state.editorDirty && !window.confirm("Discard unsaved catalog edits?")) {
    history.replaceState(null, "", lastHash);
    return;
  }
  state.editorDirty = false;
  lastHash = location.hash;
  state.personaCreating = false; state.skillCreating = false; state.skillFile = null; state.skillFileCreating = false;
  handleRoute();
});

window.addEventListener("beforeunload", (event) => {
  if (!state.editorDirty) return;
  event.preventDefault(); event.returnValue = "";
});

main.addEventListener("pointerdown", (event) => {
  if (event.target instanceof HTMLSelectElement) openSelect = event.target;
});
main.addEventListener("keydown", (event) => {
  if (
    event.target instanceof HTMLSelectElement
    && [" ", "Enter", "ArrowDown", "ArrowUp"].includes(event.key)
  ) {
    openSelect = event.target;
  }
});
main.addEventListener("change", (event) => {
  if (event.target === openSelect) openSelect = null;
  flushPendingInspectorRefresh();
});
main.addEventListener("focusout", (event) => {
  if (event.target === openSelect) openSelect = null;
  window.setTimeout(flushPendingInspectorRefresh, 0);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    openSelect = null;
    flushPendingInspectorRefresh();
  }
});

document.addEventListener("visibilitychange", () => {
  document.body.classList.toggle("page-hidden", document.hidden);
  if (!document.hidden) {
    refreshRuns({ quiet: true });
    refreshSystem({ quiet: true });
    refreshProjectQueue({ quiet: true });
    if (state.route.section === "memories") refreshMemories({ quiet: true });
    connectEvents();
    connectTelemetry();
  } else {
    telemetrySource?.close();
  }
});

if (!location.hash) history.replaceState(null, "", "#/overview");
connectEvents();
connectTelemetry();
refreshSystem({ quiet: true });
// Global state every route's chrome reads, regardless of section.
refreshRuns({ quiet: true });
// Take the same path a hashchange does rather than repeating a subset of it here. Landing directly
// on a route used to render once with no data and never fetch any, because only `runs` and
// `memories` were listed; every other route waited for the user to navigate away and back.
handleRoute();
window.setInterval(() => { if (!document.hidden) updateElapsed(); }, 1000);
