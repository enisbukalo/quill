export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function buildQuery(values = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function errorMessage(payload, fallback = "Request failed") {
  if (!payload || typeof payload !== "object") return fallback;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((item) => {
        const location = Array.isArray(item?.loc) ? item.loc.slice(1).join(".") : "request";
        return `${location || "request"}: ${item?.msg || "invalid value"}`;
      })
      .join("; ");
  }
  if (typeof payload.error === "string") return payload.error;
  return fallback;
}

export async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const init = { ...options, headers };
  if (options.body !== undefined && typeof options.body !== "string") {
    headers.set("content-type", "application/json");
    init.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError(error?.message || "Quill API is unreachable");
  }
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (response.status !== 204) {
    payload = contentType.includes("json") ? await response.json() : await response.text();
  }
  if (!response.ok) {
    const fallback = typeof payload === "string" && payload ? payload : response.statusText;
    throw new ApiError(errorMessage(payload, fallback), response.status, payload);
  }
  return payload;
}

export const segment = (value) => encodeURIComponent(String(value));
export const relativePath = (value) =>
  String(value)
    .split("/")
    .filter(Boolean)
    .map(segment)
    .join("/");

export const QuillApi = {
  health: (signal) => apiFetch("/health", { signal }),
  version: (signal) => apiFetch("/version", { signal }),
  models: (signal) => apiFetch("/models", { signal }),
  refreshModels: (signal) => apiFetch("/models?refresh=true", { signal }),
  modelSwitchState: (signal) => apiFetch("/models/switch", { signal }),
  switchModel: (modelId, force = false) =>
    apiFetch("/models/switch", { method: "POST", body: { model_id: modelId, force } }),
  unloadModel: (modelId, force = false) =>
    apiFetch("/models/unload", { method: "POST", body: { model_id: modelId, force } }),
  telemetry: (signal) => apiFetch("/telemetry", { signal }),
  telemetrySettings: (signal) => apiFetch("/settings/telemetry", { signal }),
  updateTelemetrySettings: (settings) =>
    apiFetch("/settings/telemetry", { method: "PUT", body: settings }),
  stats: (signal) => apiFetch("/stats", { signal }),
  init: (signal) => apiFetch("/init", { signal }),
  githubRepositories: (signal) => apiFetch("/github/repositories", { signal }),
  githubIssues: (repo, signal) =>
    apiFetch(`/github/repositories/${relativePath(repo)}/issues`, { signal }),
  githubIssueTitles: (repo, signal) =>
    apiFetch(`/github/repositories/${relativePath(repo)}/issue-titles`, { signal }),
  githubWorkflows: (repo, signal) =>
    apiFetch(`/github/repositories/${relativePath(repo)}/workflows`, { signal }),
  githubUpdateTarget: (repo, ticket, requireFeedback, signal) =>
    apiFetch(`/github/repositories/${relativePath(repo)}/issues/${segment(ticket)}/update-target${buildQuery({ require_feedback: requireFeedback })}`, { signal }),
  workspaces: (signal) => apiFetch("/workspaces", { signal }),
  workspaceBranches: (repo, signal) =>
    apiFetch(`/workspaces/${relativePath(repo)}/branches`, { signal }),
  pullWorkspaceBranch: (repo, branch) =>
    apiFetch(`/workspaces/${relativePath(repo)}/branches/${relativePath(branch)}/pull`, {
      method: "POST",
    }),
  deleteWorkspaceBranch: (repo, branch) =>
    apiFetch(`/workspaces/${relativePath(repo)}/branches/${relativePath(branch)}`, {
      method: "DELETE",
    }),
  memories: (signal) => apiFetch("/memories", { signal }),
  deleteMemories: (memoryIds = [], deleteAll = false) =>
    apiFetch("/memories", {
      method: "DELETE",
      body: { memory_ids: memoryIds, delete_all: deleteAll },
    }),
  queue: (signal) => apiFetch("/queue", { signal }),
  projectQueue: (signal) => apiFetch("/project-queue", { signal }),
  projectQueueCandidates: (repo, signal) =>
    apiFetch(`/project-queue/${relativePath(repo)}/candidates`, { signal }),
  addProjectQueueBatch: (repo, tickets) =>
    apiFetch(`/project-queue/${relativePath(repo)}`, {
      method: "POST",
      body: { tickets },
    }),
  removeProjectQueueItems: (repo, tickets) =>
    apiFetch(`/project-queue/${relativePath(repo)}`, {
      method: "DELETE",
      body: { tickets },
    }),
  start: (request) => apiFetch("/runs", { method: "POST", body: request }),
  runs: (filters, signal) => apiFetch(`/runs${buildQuery(filters)}`, { signal }),
  deleteRuns: (runIds) => apiFetch("/runs", { method: "DELETE", body: { run_ids: runIds } }),
  run: (runId, signal) => apiFetch(`/runs/${segment(runId)}`, { signal }),
  restartOptions: (runId, signal) => apiFetch(`/runs/${segment(runId)}/restart-options`, { signal }),
  restart: (runId, phase, sequence = null) => apiFetch(`/runs/${segment(runId)}/restart`, {
    method: "POST", body: { phase, ...(sequence == null ? {} : { sequence }) },
  }),
  breakdown: (runId, signal) => apiFetch(`/runs/${segment(runId)}/breakdown`, { signal }),
  artifacts: (runId, signal) => apiFetch(`/runs/${segment(runId)}/artifacts`, { signal }),
  artifactsArchiveUrl: (runId) => `/runs/${segment(runId)}/artifacts.zip`,
  artifactDownloadUrl: (runId, name) =>
    `/runs/${segment(runId)}/artifact-downloads/${relativePath(name)}`,
  artifact: (runId, name, signal) =>
    apiFetch(`/runs/${segment(runId)}/artifacts/${relativePath(name)}`, { signal }),
  stop: (runId) => apiFetch(`/runs/${segment(runId)}/stop`, { method: "POST" }),
  decide: (runId, answer) =>
    apiFetch(`/runs/${segment(runId)}/decision`, { method: "POST", body: { answer } }),
  personas: (signal) => apiFetch("/personas", { signal }),
  persona: (name, signal) => apiFetch(`/personas/${segment(name)}`, { signal }),
  createPersona: (name, body, reason) =>
    apiFetch("/personas", { method: "POST", body: { name, body, reason } }),
  updatePersona: (name, body, reason) =>
    apiFetch(`/personas/${segment(name)}`, { method: "PUT", body: { body, reason } }),
  deletePersona: (name, reason) =>
    apiFetch(`/personas/${segment(name)}`, { method: "DELETE", body: { reason } }),
  skills: (signal) => apiFetch("/skills", { signal }),
  skill: (name, signal) => apiFetch(`/skills/${segment(name)}`, { signal }),
  createSkill: (name, body, reason) =>
    apiFetch("/skills", { method: "POST", body: { name, body, reason, files: {} } }),
  updateSkill: (name, body, reason) =>
    apiFetch(`/skills/${segment(name)}`, { method: "PUT", body: { body, reason } }),
  deleteSkill: (name, reason) =>
    apiFetch(`/skills/${segment(name)}`, { method: "DELETE", body: { reason } }),
  skillFile: (name, path, signal) =>
    apiFetch(`/skills/${segment(name)}/files/${relativePath(path)}`, { signal }),
  writeSkillFile: (name, path, content, reason) =>
    apiFetch(`/skills/${segment(name)}/files/${relativePath(path)}`, {
      method: "PUT",
      body: { content, reason },
    }),
  deleteSkillFile: (name, path, reason) =>
    apiFetch(`/skills/${segment(name)}/files/${relativePath(path)}`, {
      method: "DELETE",
      body: { reason },
    }),
};
