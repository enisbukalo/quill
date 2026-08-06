export const ACTIVE_STATUSES = new Set(["queued", "running", "needs_decision"]);

export function parseRoute(hash) {
  const raw = String(hash || "#/overview").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
  const section = ["overview", "runs", "queue", "workspaces", "memories", "personas", "skills", "models", "settings", "api"].includes(parts[0])
    ? parts[0]
    : "overview";
  return { section, id: parts.slice(1).join("/") || null };
}

export function chooseSelection(available, current) {
  const options = (available || []).map((value) => String(value));
  if (options.includes(String(current ?? ""))) return String(current);
  return options[0] ?? "";
}

/** Workflow tags that describe the run rather than the ticket's own work. */
const WORKFLOW_LABELS = { pr_review: "PR Review", pr_update: "PR Update" };

/** "#50 Add grid coordinates…", "#49 PR Review", or "#49" when no title is known.
 *
 * A PR review or update run is not doing the ticket's work, so repeating the issue title there
 * says less than naming what the run actually is. Falls back to the bare number rather than
 * inventing a name when the repository's titles have not loaded.
 */
export function ticketLabel(run, titles = {}) {
  const number = `#${run?.ticket ?? "?"}`;
  const workflow = WORKFLOW_LABELS[run?.workflow];
  if (workflow) return `${number} ${workflow}`;
  const title = titles?.[String(run?.ticket)];
  return title ? `${number} ${title}` : number;
}

export function queueCapableRepositories(repositories) {
  return (repositories || []).filter((repository) => (
    typeof repository?.project_board === "string" && repository.project_board.trim()
  ));
}

export function pruneQueueSelection(groups, selected) {
  const eligible = new Set(
    (groups || []).flatMap((group) => (group.tickets || []))
      .filter((ticket) => ticket.selectable)
      .map((ticket) => String(ticket.number)),
  );
  return new Set([...selected].map(String).filter((ticket) => eligible.has(ticket)));
}

export function groupSelectionState(group, selected) {
  const eligible = (group?.tickets || []).filter((ticket) => ticket.selectable);
  const count = eligible.filter((ticket) => selected.has(String(ticket.number))).length;
  return {
    checked: eligible.length > 0 && count === eligible.length,
    indeterminate: count > 0 && count < eligible.length,
    disabled: eligible.length === 0,
  };
}

/** Whole-number display for counts and token totals.
 *
 * Every caller passes a count or a token total, so a fraction is never meaningful here. Derived
 * values are what made this visible: a chart's midpoint tick and its average are computed, not
 * counted, so the default formatter rendered "278,373.5" tokens on an axis and "278,161.167" as
 * an average.
 */
export function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat().format(Math.round(number)) : "—";
}

export function formatMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "$0.00";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 5,
  }).format(number);
}

export function diagnosticSummary(value, limit = 180) {
  const lines = String(value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const meaningful = lines.find((line) => /(?:fatal error|\berror:|\bfailed:|exception)/i.test(line));
  const summary = meaningful || lines[0] || "No diagnostic details were reported.";
  return summary.length > limit ? `${summary.slice(0, limit - 1)}…` : summary;
}

export function formatBytes(value) {
  let number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  return `${number.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatMemoryGb(valueMb) {
  if (valueMb === null || valueMb === undefined || valueMb === "") return "—";
  const number = Number(valueMb);
  return Number.isFinite(number) && number >= 0 ? (number / 1024).toFixed(2) : "—";
}

export function clampPercent(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : null;
}

export function formatPercent(value) {
  const number = clampPercent(value);
  return number === null ? "N/A" : `${number.toFixed(0)}%`;
}

export function formatTemperature(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}°C` : "N/A";
}

export function temperatureTone(value, cool = 45, hot = 85) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "unavailable";
  if (number >= hot) return "hot";
  if (number >= cool) return "warm";
  return "cool";
}

export function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds));
  if (!Number.isFinite(value)) return "—";
  const total = Math.floor(value);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(secs).padStart(2, "0")}s`;
  if (minutes) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

export function formatTime(epoch) {
  const value = Number(epoch);
  if (!Number.isFinite(value) || value <= 0) return { label: "—", iso: "" };
  const date = new Date(value * 1000);
  return {
    label: new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(date),
    iso: date.toISOString(),
  };
}

export function runElapsed(startedAt, finishedAt = null, now = Date.now() / 1000) {
  const started = Number(startedAt);
  const finished = finishedAt === null || finishedAt === undefined ? now : Number(finishedAt);
  return Number.isFinite(started) && started > 0 && Number.isFinite(finished)
    ? formatDuration(Math.max(0, finished - started))
    : "—";
}

export function statusTone(status) {
  if (["done", "completed", "enabled", "passed", "PASS", "DONE"].includes(status)) return "success";
  if (["disabled", "failed", "halted", "paused", "BLOCK", "FAILED", "CRASH"].includes(status)) return "danger";
  if (status === "needs_decision") return "warning";
  if (["running", "queued", "waiting_pr"].includes(status)) return "active";
  return "neutral";
}

export function liveRunLabel(run) {
  const status = run?.status || "idle";
  if (status === "running") return run.activity_label || run.phase_label || run.phase || "Pipeline running";
  if (status === "queued") return "Queued for execution";
  if (status === "needs_decision") return "Operator decision required";
  if (status === "done") return "Run completed";
  if (status === "failed") return "Run failed";
  if (status === "halted") return "Run halted";
  return "Awaiting the next run";
}

export const canStopRun = (status) => ACTIVE_STATUSES.has(status);
export const canAnswerRun = (status) => status === "needs_decision";

export function branchName(workType, issue) {
  const prefix = String(workType || "feat")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "feat";
  const slug = String(issue?.title || `ticket-${issue?.number || "work"}`)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80)
    .replace(/-+$/g, "");
  const ticket = String(issue?.number || "work");
  return `${prefix}/${slug || `ticket-${ticket}`}_${ticket}`;
}

export function preferredWorkType(issue, workTypes) {
  const labels = new Set(issue?.labels || []);
  const available = new Set(workTypes || []);
  for (const candidate of [
    "bug",
    "fix",
    "enhancement",
    "feature",
    "feat",
    "refactor",
    "chore",
    "documentation",
    "docs",
    "ci",
    "test",
  ]) {
    if (labels.has(candidate) && available.has(candidate)) return candidate;
  }
  return workTypes?.[0] || "feat";
}

export function validCatalogName(value) {
  const name = String(value || "").trim();
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(name) && !name.includes("..");
}

export function validReason(value) {
  const length = String(value || "").trim().length;
  return length >= 3 && length <= 200;
}

export function safeExternalUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}
