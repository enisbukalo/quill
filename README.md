# Quill

[![CI](https://github.com/enisbukalo/quill/actions/workflows/ci.yml/badge.svg)](https://github.com/enisbukalo/quill/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/enisbukalo/quill)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg?logo=python&logoColor=white)](https://docs.python.org/3.12/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-1.27%2B-7C3AED.svg)](https://modelcontextprotocol.io/)
[![Stars](https://img.shields.io/github/stars/enisbukalo/quill?style=flat)](https://github.com/enisbukalo/quill/stargazers)

Quill is a domain-specific **execution-graph engineering system** for LLM software delivery. It
turns a repository's workflow definition into an executable graph whose agentic nodes research,
plan, implement, review, and publish work while deterministic nodes enforce build, test, and CI
policy. Models own judgment and code changes; Quill owns topology, evidence flow, scheduling,
gates, retries, recovery, and durable state.

A typical ticket graph looks like this:

```
workspace prep → concurrent research/gate → plan/gate → implement
               → test/build → concurrent implementation audits/gate → commit/push/PR
```

More precisely, Quill is a **workflow graph with agentic nodes**. The repository defines and
versions the allowed routes in `quillfolio.toml`; persona and skill files independently define how
each model-driven node works. The graph supports serial stages, bounded parallel fan-out, synthesis,
evidence dependencies, mechanically authoritative gates, and retry back-edges. Quill validates a
constrained software-delivery topology rather than accepting an arbitrary general-purpose graph,
which keeps artifacts, traversal, and recovery predictable.

This is execution-graph engineering, not knowledge-graph engineering or GraphRAG. Quill's graph
represents what executes and under which conditions, not entities and semantic relationships used
for retrieval. See the [architecture wiki](https://github.com/enisbukalo/quill/wiki/Architecture)
for the complete graph model.

Adding, removing, reordering, or renaming a phase is normally **configuration + persona text, zero
driver code**. One run owns the model stack at a time, while compatible vLLM phases may use multiple
concurrent Pi sessions up to live model capacity. The target repository selects its workflow graph,
commands, personas, and skills; reusable persona and skill bodies live in configured libraries. The
same engine can therefore ship unrelated projects.

Quill supports two operating modes built on that engine:

- `quill` runs locally against the current Git repository.
- `quill-api` owns persistent server workspaces and exposes HTTP, SSE, a web control center, and
  the stateless `quill-mcp` adapter.

## Quick Start

Set up quill locally in any repo in five steps:

```bash
# 1. Install quill (requires Python ≥ 3.12)
uv tool install "quill[api,mcp] @ git+https://github.com/enisbukalo/quill@main"

# 2. Authenticate GitHub CLI
gh auth login

# 3. Bootstrap your repo
cd /path/to/your-repo
quill --init        # writes quillfolio.toml + seeds your persona library

# 4. Fill in the three required fields in quillfolio.toml:
#    [runner]
#    kind = "pi"              # or "opencode"
#
#    [build]
#    command = "cargo build"  # your build command
#    test    = "cargo test"   # your test command
#
# 5. Run a ticket
quill 42
```

That's it. `--init` gives you a working pipeline with sensible defaults. Edit the config
and personas to match your project, then run for real:

```bash
quill 42                    # full ship: workspace prep → … → PR
quill 42 --update           # revise an open PR against its review comments
quill 42 --start-phase impl # skip ahead to a specific phase
```

See [Configuration](#configuration) and [Personas](#personas) for details.

To run the same repository through a shared Quill server instead, keep `quillfolio.toml` in the
repository and point the client at the service:

```bash
quill 42 --server http://quill-box:8002
# or set once for the shell:
export QUILL_SERVER=http://quill-box:8002
quill 42
```

The client sends the repository, current branch, ticket, and mode. The server loads the committed
`quillfolio.toml` from the root of its checkout and owns GitHub authentication, the model stack,
coding-agent CLI, personas, run queue, and history.
Use `GET /init` when an agent needs the server's live config schema and available catalogs.

Hosting Quill for the first time? Follow the ordered
[fresh-machine checklist](docs/setup/server.md#fresh-machine-checklist). It distinguishes the state
Quill creates automatically from the model server, coding-agent CLI, GitHub authentication,
persona/skill libraries, service configuration, and repository toolchains the operator must provide.

## Install

quill is a **CLI you run from inside other repos**, so install it as a tool (globally on PATH),
not as a dependency of the project you're shipping. Requires Python ≥ 3.12.

### With uv (recommended)

```bash
uv tool install "quill[api,mcp] @ git+https://github.com/enisbukalo/quill@main"
```

Puts `quill`, `quill-api`, and `quill-mcp` on your PATH (`~/.local/bin`) in their own isolated
environment. Drop optional extras you do not need: `api` is the FastAPI service and `mcp` is the
fire-and-forget stdio MCP bridge.

### With pip

```bash
pip install "quill[api] @ git+https://github.com/enisbukalo/quill@main"
```

Works, but installs into whatever environment is active — which is rarely what you want for a
tool you invoke from other repos. Prefer `uv tool install` or `pipx install`.

### Dev install (working on quill itself)

Install it **editable** so the `quill` on your PATH runs your working tree — edit the source and
the next `quill` invocation picks it up, with no reinstall step:

```bash
git clone https://github.com/enisbukalo/quill && cd quill
uv tool install --editable ".[api,mcp]"    # tools on PATH now point at this checkout
uv sync --extra dev --extra api --extra mcp # dev environment for the test suite
uv run pytest                          # run the tests
```

The automated suite is pytest with temporary directories and deterministic fake Pi, Git, and
build runners. It does not start vLLM or spend model tokens. A live-model smoke run is a separate,
optional operational check; it is not part of pytest and should use a disposable ticket.

Check what your `quill` is actually wired to at any time:

```bash
uv tool list                                        # installed tools + versions
cat "$(uv tool dir)/quill/uv-receipt.toml"          # shows `editable = "/path/to/checkout"`
```

If that receipt shows an `editable` path, **your source edits are already live — there is
nothing to reinstall.** Re-run `uv tool install --editable ".[api]"` only when entry points or
dependencies change (a new console script, a new package in `[project.dependencies]`).

<details>
<summary>pip equivalent</summary>

```bash
git clone https://github.com/enisbukalo/quill && cd quill
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev,api]"
pytest                       # run the test suite
```

This puts `quill` on PATH only while that venv is active.

</details>

## Prerequisites

You provide these — quill detects but never installs them:

- **[GitHub CLI](https://cli.github.com)** on PATH and authenticated: `gh auth login`.
  quill preflights `gh` and fails fast with a clear message if it's missing or logged out.
- A **model server** — either the [llama.cpp router](https://github.com/ggerganov/llama.cpp)
  on `http://localhost:8001`, or a [vLLM](https://docs.vllm.ai/) server. Configure which in
  `quillfolio.toml` (`[runner] backend`). vLLM deployments must set the machine-level
  `QUILL_VLLM_URL` environment variable to the server's base URL.
- A **coding-agent CLI** on PATH — quill drives each phase through one. Built-in support for
  [`opencode`](https://opencode.ai) and [`pi`](https://pi.dev); pick which in
  `quillfolio.toml` (`[runner] kind`). You configure that CLI's own models/providers
  to point at your model server; quill just invokes it and reads its result. Adding another CLI
  is one `quill/runners/` subclass — see [`quill/runners/`](quill/runners/).
- A **`quillfolio.toml`** in the target repo. Run `quill --init` once to create it. See
  [`docs/setup/`](docs/setup/) for the machine-side config (models, providers, permissions).

quill operates on the **current directory** — `cd` into the target repo first. It preflights
that the repo is a git repo with an `origin` remote and fails fast otherwise. There is no
`--dir`/`--repo`: the repo is cwd and the remote is derived from it.

## Configuration

`quill --init` creates `quillfolio.toml` at the repo root. The starter configuration includes the
following machine and policy settings before its named workflow graphs:

```toml
[repo]
pr_base = "main"          # base branch for PRs
excluded_issue_labels = ["EPIC"] # omit matching issues from the web run form
# pr_checks_required = false # permit an empty PR check rollup; reported failures still block

[runner]
kind = "pi"               # REQUIRED: "pi" or "opencode"
backend = "vllm"          # starter workflows use concurrent vLLM lanes

# Optional local-runner model service switching:
# [runner.vllm]
# command = ["sudo", "systemctl", "start"]
#
# [runner.vllm.models]
# model-id = "service-name"

[build]
command = "cargo build"   # REQUIRED: your build command
test    = "cargo test"    # REQUIRED: your test command
log_dir = "logs"

[retries]
default = 1               # revise-then-verify rounds per gated phase, per run
spawn   = 1               # re-spawns on CRASH / GARBAGE

[gates]                              # which findings stop a gate, per revise round
blocking_severities = ["CRITICAL", "MAJOR"]  # round 0 (the initial review)
retry_blocking      = "same"                 # or "repeat-only" (see below)
final_round         = ["CRITICAL", "MAJOR"]  # last round the budget allows

[memory]
enabled = false           # opt in to repository-scoped, verified blocker memory

[timeouts]
opencode_run_seconds = 5400 # full phase and same-session continuation hang guard
model_load_seconds = 360  # wait up to six minutes for a model service switch
```

Every configured phase publishes an exact versioned contract. An LLM phase first completes its
natural work artifact without seeing the contract schema, then performs its phase-specific
same-session self-check. Quill freezes that checked artifact and repository identity before sending
one final serialization-only continuation. Only that late projection sees the payload shape. Quill
validates it, binds immutable evidence hashes and exact upstream contract attempts, and atomically
publishes `contracts/<phase>/attempt-N.json` plus a latest pointer. Projection may never edit the
frozen artifact or repository.

Contracts carry kind/version, `COMPLETE`/`INCOMPLETE`/`PARTIAL`/`UNAVAILABLE` status, phase and run
identity, spec digest, artifact hashes, Git/worktree identity, exact upstream refs, payload, and a
canonical envelope digest. Downstream phases accept exact versions and require `COMPLETE` inputs.
`INCOMPLETE` records a concrete semantic omission and starts a fresh schema-blind phase attempt; it
is never published as a successful handoff. Mechanical contracts are built by Python from observed
commands, exit codes, GitHub checks, SHAs, and hashed logs rather than model claims.

For malformed projection output, Quill sends only the structural validation problem back to
the same Pi session. If that bounded self-fix still returns `GARBAGE`, Quill starts a fresh phase
session up to `[retries].spawn` times. Explicit `FAILED:` and `BLOCK:` verdicts remain authoritative;
they are not treated as formatting failures. A nested retry phase consumes its own spawn budget, so
its failure cannot cause the parent gate to restart without completing the required repair route.
Self-fix attempts are durable run events. The phase graph shows an inactive purple loop until a
repair is needed, blue while the repair is active, green after deterministic revalidation accepts
the corrected output, and red when repair fails before a fresh phase attempt.

With the vLLM backend, every phase model must exactly match an ID advertised by
`GET /v1/models`. If it is not loaded, Quill appends the associated service name to
`runner.vllm.command`, starts it without a shell, and polls until that exact model appears. Actual
service switches are durable, timed run operations; already-resident model checks do not create a
load record or add time to the configured phase that follows.

### Workflows and phases

Repositories can store multiple complete graphs under `[workflows.<id>]` with phases in
`[[workflows.<id>.phase]]`; `[workflows] default = "ticket"` selects the normal graph. The shipped
template defines `ticket` for new work, `pr_update` for bounded follow-ups, and `pr_review` for an
independent read-only merge gate. Legacy root
`[[phase]]` remains accepted as one synthetic `ticket` workflow, but it cannot be mixed with named
graphs. Four phase types are available:

| Type | What it does | Key fields |
|---|---|---|
| `producer` | LLM writes an artifact; consecutive same-model producers can run concurrently | `persona`, `model`, `artifact`, `produces_contract`; optional `inputs`, `requires`, `parallel_group`, `synthesizes` |
| `reviewer` | LLM judges artifacts; `models=[...]` fans out sequentially, or vLLM `[[phase.audits]]` runs named same-model lanes concurrently | `persona` + `models`, or `audits`; `against`, `gates`, contract fields |
| `finalizer` | LLM reconciles N reviewers' findings, applies gate | `persona`, `model`, `reconciles`, `gates`, contract fields |
| `mechanical` | Python records typed observed evidence without a model | `step`, contract fields |

When a gating phase `BLOCK`s, `on_block` sends execution back to one earlier phase and normal
phase traversal resumes from there, up to `retry_budget` times. Each reviewer's findings use
severity tags (CRITICAL/MAJOR/MINOR/NIT) —
only CRITICAL and MAJOR block; MINOR and NIT are advisory.

`retry_budget` is a **per-run ceiling**, not a per-attempt one. The gate loop runs inside the same
wrapper that starts a fresh phase session after `CRASH`/`GARBAGE`, so rounds are tallied across the
whole run: a gate that re-enters resumes its remaining rounds rather than receiving a fresh budget.

#### Converging gates

A gate that applies one severity set to every round is not guaranteed to terminate. Reviewers
re-reading revised code derive findings afresh, so a round can resolve every blocker it was given
and still be stopped by new ones of the same severity — indefinitely, since "some path is untested"
is an unbounded source of MAJOR findings on any codebase.

`[gates]` makes blocking a function of the round rather than of severity alone:

| Round | `retry_blocking = "same"` (default) | `retry_blocking = "repeat-only"` |
|---|---|---|
| 0 — initial review | `blocking_severities` | `blocking_severities` |
| 1..N-1 — revise rounds | `blocking_severities` | CRITICAL, plus findings an earlier round already reported |
| N — final round | `final_round` | CRITICAL, plus findings an earlier round already reported |

Under `repeat-only` a retry may only be blocked by something the producer was already told about,
or by a CRITICAL. The blocking set is therefore monotonically non-increasing: the loop drains to
`PASS` or halts on a specific defect that was reported repeatedly and not fixed. Late discovery is
recorded as advisory instead of consuming another retry. CRITICAL is exempt on every round, so a
revision that breaks the build or crashes still stops the run.

Two other behaviors follow the same principle. Reviewers re-run inside a gate's revise route are
prompted in verification mode against that gate's carried findings, rather than auditing fresh. And
a gating re-review reports a **status delta** — `dispositions` naming prior finding IDs, plus any
`new_findings` — which Quill applies to the records it already holds. The model never re-emits a
prior finding's immutable fields, so verification cannot fail on transcription drift.

Consecutive vLLM producers with the same non-empty `parallel_group` run as independent sessions,
bounded by the model's live capacity. Each lane emits its own live start, usage, tool, completion,
and phase-history events. Quill preserves a canonical latest artifact plus immutable attempt
snapshots in `parallel-<group>-manifest.json`. A serial producer with `synthesizes = [...]` rebuilds
one handoff from the latest lane artifacts.

A selective gate has two supported forms. Direct mode sets `selective_on_block = [lane ids]`, names
every lane in `against`, and omits `on_block`; after selected lanes rerun, the gate re-reviews all
latest lane contracts. Synthesis compatibility mode combines the same selective list with
`on_block = "synthesis"`; selected lanes rerun, the serial synthesis rebuilds from every latest
lane, and then the gate verifies.

Every open Critical/Major finding must name one allowed `owner`; Quill reruns only the lanes that own
blockers and runs those selected lanes concurrently when capacity permits. Configuration validation
requires the selective list to cover one complete parallel group exactly once. On verification, a new blocker can extend the
retry loop only when `introduced_by_revision` identifies the revision that caused it; a late discovery
of an older issue remains advisory.

Set `structured_findings = true` on a gated LLM reviewer or finalizer to make the validated late
projection—not the model's receipt—the verdict authority. The reviewer writes and self-checks natural
notes; only the final continuation projects stable IDs, severities, evidence, required outcomes, and
status. Quill blocks on every open CRITICAL/MAJOR finding and requires prior blocker identities to
survive verification. A contradictory `PASS:` receipt cannot advance the workflow.

When `[memory] enabled = true`, Quill mechanically archives each gate's compact BLOCK receipt in
`~/.quill/memory/<owner>/<repo>/blockers.jsonl`. A finding becomes reusable only after the existing
revise/verify loop reaches PASS; unresolved blockers remain historical evidence but are never
injected. Every unique verified finding is then supplied to research lanes and synthesis/gate,
`plan`, `review_plan`, `impl`/legacy `impl_finalize`, `review_impl_final`, and the architecture,
correctness, and tests audit lanes. It is deliberately
excluded from local build/test, CI, and PR phases. The current repository, ticket, official docs,
and tests remain authoritative over remembered findings.

The dashboard's **Memories** page lists verified lessons across repositories, filters them by
repository, and supports selected or complete deletion. `GET /memories` provides the same listing;
`DELETE /memories` accepts `memory_ids` or `delete_all=true`. Complete deletion also clears raw
unresolved blocker events, while unresolved events are never presented as reusable memories.

For a horizontal implementation review, omit the parent reviewer's `persona` and `models` and
add two or more nested `[[phase.audits]]` tables. Each audit has its own `id`, `label`, `persona`,
`model`, and optional `skills`; every audit must use the same vLLM model. Quill loads that model
once and bounds the Pi sessions to the model's live capacity. Pi's `subagentConcurrency` is child
capacity, so Quill adds the parent slot, subtracts root Pi processes already running at phase entry,
and safely uses one lane when the registry or process snapshot is unavailable. Excess lanes wait;
each starts as soon as a slot opens, reports completion immediately, and the following finalizer
still waits for every lane before reconciling the single gate verdict. A BLOCK back to the
configured implementation retry phase re-enters normal traversal, takes a fresh capacity snapshot,
and runs all audit lanes again before the gate re-evaluates.

Tool calls and model output remain unlimited because productive repository work varies too widely
for either count to identify a runaway reliably. Process timeouts and explicit stop requests remain
the safety boundaries.

`max_artifact_chars` is available on any LLM phase to bound the artifact handed to later phases, but
**no shipped configuration sets it**. A cap costs twice over: an oversized artifact spends a
same-session compaction turn before it is revalidated, and — more insidiously — models write up to
whatever ceiling they are given, cutting real detail from the handoff to stay under it. Set one only
if a downstream consumer genuinely cannot take the size. Quill never silently truncates. Producer
`inputs = ["research_requirements", "research_architecture", "research_technical"]` names the
exact earlier artifacts the task must read,
preventing broad run-directory archaeology and accidental context growth.

For Pi phases, `self_check = true` or a named self-check persona adds one completion audit after the
natural task succeeds and before projection. Shipped workflows enable it on every consumed LLM phase.
Quill reopens the exact Pi conversation and asks the worker to verify its natural phase work,
required skills, accuracy, unsupported claims, and obvious omissions, then validates the artifact
and receipt again.
The self-check sees no schema and cannot broaden the original phase's mutation authority. Mechanical
phases do not use a model. The configured 5,400-second continuation timeout is unchanged.

When a Pi worker omits its final receipt or writes an invalid one, Quill continues that exact session
once. The recovery prompt requires the model to inspect its work and continue if incomplete; an
artifact existing on disk is not treated as proof of `DONE` or `PASS`.

### Adding a custom phase

Add a `[[workflows.<id>.phase]]` entry and write a persona `.md` in your persona library. No driver code
change needed. See [Personas](#personas) below.

## Personas

Personas are plain `.md` files in a machine-level library (`~/.quill/personas`) that define
**what each phase does**.
Each is a system prompt — instructions for the LLM playing that role. `quill --init` creates
the defaults below:

| File | Role |
|---|---|
| `research.md` | Investigate the ticket and repository before planning; write a bounded evidence handoff |
| `research-requirements.md` | Trace ticket requirements, exclusions, current behavior, and observable outcomes |
| `research-architecture.md` | Trace ownership, dependencies, state flow, and lifecycle constraints |
| `research-technical.md` | Verify versioned external contracts and executable validation seams |
| `research-synthesis.md` | Reconcile the current research lanes into one planning handoff |
| `review-research.md` | Gate the direct research lanes and assign defects to their owning lane |
| `plan.md` | Write an implementation plan with phased, self-contained steps |
| `review-plan.md` | Judge the plan — severity-tagged findings, gate on CRITICAL/MAJOR |
| `impl.md` | Apply a complete change from the plan, never build/test |
| `impl-core.md` | Implement foundational contracts and core behavior |
| `impl-integration.md` | Connect the core through callers, lifecycle, and persistence |
| `impl-finalize.md` | Reconcile tests and returned review/build/CI findings across every layer |
| `review-impl.md` | Independent review of the implementation against the plan |
| `review-impl-architecture.md` | Audit ticket coverage, ownership, dependencies, and architecture |
| `review-impl-correctness.md` | Audit production correctness and lifecycle behavior |
| `review-impl-tests.md` | Audit behavioral tests and regression coverage |
| `review-final.md` | Reconcile reviewer findings, apply gate rule |
| `update-scope.md` | Convert active pull-request feedback into a bounded update scope |
| `update-impl.md` | Implement every active feedback item on the existing pull-request branch |
| `review-update.md` | Gate only the changes made after the captured feedback boundary |
| `pr-review-requirements.md` | Audit ticket and acceptance-criteria coverage in an existing PR |
| `pr-review-correctness.md` | Audit correctness, failure behavior, and regression risk |
| `pr-review-architecture.md` | Audit repository architecture and integration contracts |
| `pr-review-final.md` | Reconcile PR audits into validated `PASS`/`BLOCK` JSON |
| `commit.md` | Commit, push, open (or update) the PR |

To customize a persona, edit the `.md` file directly. To add a new one, create a new `.md` and
reference it from a `[[workflows.<id>.phase]]` entry. Personas are shared by every repo on the
machine; a repo picks which ones it wants by naming them (`persona = "impl-cpp.md"`), so improving
one improves every workflow that uses it.

## CLI Reference

```bash
quill --init                  # write quillfolio.toml + seed the persona library

quill 42                      # full ship: workspace prep → … → PR
quill 42 --update             # revise the ticket's open PR from its review comments
quill 42 --resume             # pick up the latest halted run from its saved state
quill 42 --start-phase impl   # start from a configured phase id (not an int)

quill 42 --server http://quill-box:8002   # run it on a remote quill server
quill 42 --server ... --branch my-branch   # pick the branch (default: current)
```

## Running quill as a service

`quill-api` serves every repo from one machine. Clients send `{repo, branch, ticket, mode}` and
need no GPU, model server, agent CLI, or `gh` auth of their own — the work happens server-side and
the events stream back, printing exactly as a local run.

```bash
export QUILL_VLLM_URL=http://vllm.example:8000
quill-api                     # listens on :8002 (QUILL_HOST / QUILL_PORT)

# POST /runs {"repo":"me/proj","branch":"feat/x_42","ticket":42,"workflow":"ticket","mode":"create"}
# GET  /queue · GET /project-queue · GET /runs/{id} · POST /runs/{id}/stop
# GET  /events (SSE) · GET /runs/{id}/artifacts · GET /runs/{id}/artifacts.zip
# GET  /init            everything an agent needs to write a repo's config
# GET/POST/PUT/DELETE /personas · /skills   (every write needs a `reason`; it becomes the
#                                            commit message when the library is a git repo)
```

The web start form can override models for one run. `POST /runs` accepts a `model_overrides`
object whose keys are phase IDs and whose values are model IDs. Quill validates the overrides
against the selected workflow and the exact branch configuration, applies them to an in-memory
configuration copy, and never changes `quillfolio.toml`. A concurrent audit phase keeps all of
its audit lanes and assigns the selected model to every lane.

The `pr_review` workflow resolves the ticket's open PR remotely, fetches its branch, checks out the
exact PR head, and never requires a pre-existing local workspace. It records configured test and
build output, then runs requirements, correctness, and architecture audits at the model's available
concurrency. A finalizer emits `pr-review.json`; Quill Python validates every finding, rejects
anything below `MAJOR`, verifies that the PR head did not move and the checkout remained unchanged,
then creates or updates one `<!-- quill-pr-review -->` PR comment. A clean review posts an explicit
checked-by-Quill result, revalidates the reviewed SHA, branch, base, merge state, and repository
check policy, merges with a merge commit, then deletes only the remote feature branch. The
persistent local workspace branch remains available. Review findings do not make the Quill run
itself fail: `BLOCK` is the PR's merge-readiness result, while the run completed its job.

The API watches configured repositories whose default-branch TOML defines `pr_review`. By default,
Quill queues one review for an exact non-draft PR head only after every check in its nonempty rollup
completes successfully. `[repo].pr_checks_required = false` also admits an explicitly empty rollup
for repositories that use Quill's local verification instead of PR Actions. Malformed check data
and failed, canceled, timed-out, skipped, neutral, or pending reported checks remain ineligible.
Before the ticket workflow's CI gate reads those checks, Quill verifies GitHub's
parsed closing-issue references. If the selected ticket is missing, Quill preserves the PR body,
appends `Closes #<ticket>`, and verifies that GitHub resolved the link; failure stops the run. The
watcher requires exactly one closing ticket. Repeated polls and server restarts do not duplicate a
reviewed head; a later commit is a new review boundary. Automatic reviews wait behind an active run.
Every completed review creates or updates the managed comment, including a clean `PASS` stating that
Quill Pull Request Reviewer checked the revision.

When PR feedback automation is enabled, a validated `BLOCK` result queues one `pr_update` run
against the exact reviewed SHA. The update must push a different head; the watcher admits its next
review when that head satisfies the same repository check policy. A validated `PASS` closes the
cycle by merging the exact reviewed head and deleting its remote feature branch.
Persistent cycle records prevent duplicate dispatch after polling or restart, stale feedback is
cancelled if the head moves before dispatch, and a configurable cycle limit stops an unresolved PR
from looping indefinitely. Quill comments never trigger this flow by themselves: only a validated
review result stored by the completed `pr_review` run can queue an update.

Runs execute one at a time—the GPU is exclusive—and a manual second submission is rejected with 409
while a run is queued, running, or awaiting a decision. Automatic PR reviews wait in the internal
queue. See [docs/setup/server.md](docs/setup/server.md) for systemd, firewall rules, and how to
let GitHub Actions do the building so the server needs no per-repo toolchain.

The HTTP service currently has no application-layer authentication. Treat network reachability as
an administrative security boundary and follow the deployment guide's firewall requirements.

### Web control center

Open `http://quill-box:8002/` for the built-in dark synthwave dashboard. It shows health, model
reachability, the active queue, recent and historical runs, ordered phase breakdowns, and run
artifacts. Dedicated resource telemetry shows CPU identity/load/temperature/RAM and each NVIDIA
GPU's identity/load/temperature/VRAM. The Settings page persists CPU and GPU temperature-bar ranges
in the history database; defaults are 20–70°C for CPU and 20–80°C for GPU. Live run state is
push-first through `/events`, with bounded REST refetches for startup and reconnect recovery.
The System Telemetry header reports the model advertised by vLLM plus rolling processing and
generation throughput. Quill samples vLLM's Prometheus counters every 250 ms and averages the
latest 100 active-work intervals; idle polls preserve the last average, while a model change or
counter reset clears the window. If vLLM stops advertising usable metrics because the model was
unloaded or the server became unreachable, Quill immediately clears the model name and both rate
windows instead of retaining the last loaded model's values.
The Models page can also unload the resident model. The API runs `sudo systemctl stop <unit>` by
default, verifies that vLLM no longer advertises the model, and protects active or queued runs with
the same explicit force confirmation used for model switching. Override the machine command with
`QUILL_VLLM_STOP_COMMAND` when required.

Production installations can set `QUILL_WEB_ROOT` to an external dashboard release and update it
with `./scripts/deploy-web.sh`. The versioned asset swap is atomic and does not restart the API or
interrupt an active run; API/Python deployments remain a separate service restart.

The dashboard can start runs, stop them, answer `needs_decision`, manage verified blocker memories,
and fully manage the shared persona and skill catalogs (including auxiliary skill files). Run submission names only the repo,
ticket, workflow, and server-derived branch; configuration is loaded from the checked-out repository
root. At startup the server uses GitHub metadata to cache only source repositories whose default
branch contains a root `quillfolio.toml`, without cloning or fetching. Create workflows generate
`<work-type>/<kebab-case-issue-title>_<ticket>`. Update workflows resolve exactly one open PR from
the ticket and require that exact PR head branch to already exist in the server's local checkout.
Run-history filters apply immediately. The Runs page uses 200-row pages; Overview's independent
Recent Runs table uses 25-row pages. Selecting a table row opens its run without a second selector.

Failed and halted new-ticket runs can restart from a saved phase boundary when their local branch
is ahead of `origin/main`. If `main` advanced after the run began, Quill compares its changed files
with the run's changed files. Non-overlapping advances remain eligible and are merged into the
restored checkpoint before execution; overlapping paths block restart. Quill also refuses restart
after any PR has used the branch or when the run was merged, diverged, or lost its local checkout.
Before every phase invocation, including loop re-entry, Quill commits a local-only checkpoint and
records its SHA and capture time. Every safely matched execution in the run's phase-history table
has its own confirmed **Apply** action; concurrent audit rows re-enter their configured parent
phase. A restart restores that boundary and computes the chosen phase's transitive upstream contract
closure. It revalidates the phase-set/spec/envelope digests, exact attempts, source run and checkpoint
identity, and every bound artifact hash, then copies only that immutable closure plus allowlisted
matching transcripts. A partial, stale, or unrelated closure is rejected. Completed phase rows,
graph routes, timings, usage, tool calls, and self-check/model-load state remain visible as inherited
history. The linked run also inherits the source run's effective per-phase
model overrides instead of reverting to current config defaults. New execution is appended to that
lineage, and inherited transcripts are never overwritten. Historical attempts without a provable
checkpoint association remain visible but are not offered as restart boundaries. The current
workflow definition still controls the resumed execution. Private checkpoint commits are removed
before the PR delivery phase, so they are never pushed. Starting the same ticket normally discards
only a Quill-owned failed/halted branch with no PR; unknown local branches remain protected.

Lifetime run outcomes, elapsed time, token usage, cost, per-model totals, and actual model-load
counts and duration are stored in a permanent SQLite accounting ledger. The run graph inserts a
timed model-load node before the waiting phase only when a backend switch occurred. Deleting
retained run details removes their searchable row, cached breakdown, and artifacts but does not
subtract that run from lifetime statistics. On a
restart, an interrupted run is finalized from its flushed event/transcript files before being
added to the ledger. Terminal failures also carry stable machine-readable categories, including
lost vLLM connectivity, timeouts, local test/build failures, CI failures, configuration errors, and
workspace errors. The dashboard presents the category before the preserved raw diagnostic, and
the permanent ledger retains category totals after run details are deleted.

When `[repo].project_board` names a GitHub Project, Quill—not the model—moves the selected issue to
`In progress` after the run workspace and configuration are ready. A successful final PR phase
(`commit` for create runs or `commit_update` for update runs) moves it to `In review`. Missing or
unavailable project metadata does not fail the implementation run.

The dashboard's **Queue** page adds unattended, fail-closed ticket sequencing on top of those board
transitions. It reads native GitHub parent/sub-issue relationships, groups child tickets under
epics, and never queues an epic itself. Each **Add To Queue** action is one FIFO batch; tickets are
numeric within the batch, and the entire oldest batch finishes before a later batch starts. Quill
waits for the Project `Queue` snapshot to remain unchanged for five seconds, admits only its durable
head, and retains that ticket through implementation, CI, PR review, updates, and re-review. Only
an exact PR verified as `MERGED` moves the ticket to `Done` and releases the next ticket. Failed,
halted, closed-unmerged, or ambiguous work pauses the head. Pending board removals are honored;
active work is never cancelled by a board edit. SQLite retains batches and ownership across API
restarts.

`GET /project-queue` is this end-to-end ticket backlog. `GET /queue` and the unchanged MCP
`quill_queue` tool report only admitted/executing runs. The Overview shows the durable ticket order
and a clickable live Current Phase snapshot; the dedicated Queue page owns selection and batch
details.

### Asynchronous MCP control

`quill-mcp` is a stdio MCP server for agents that should kick off work and return control instead
of holding a session open. It exposes exactly eight point-in-time tools:

- `quill_start`: queue a ticket and return its durable run ID immediately; Quill clears vLLM's
  prefix cache once before the first prompt
- `quill_status`: inspect a run by ID or rediscover the newest run for a checkout/ticket
- `quill_run_breakdown`: return compact chronological statistics for every phase invocation
- `quill_recent_runs`: list recent work for later-session discovery
- `quill_queue`: view the active run and ordered waiting queue
- `quill_stop`: cancel queued work immediately or request an active boundary stop
- `quill_restart`: start an eligible failed/halted run from a saved phase boundary
- `quill_answer`: answer a run parked in `needs_decision`

There is intentionally no wait, follow, polling, or SSE MCP tool. Set the API URL and launch it:

```bash
export QUILL_SERVER=http://quill-box:8002
quill-mcp
```

`quill_start` executes the selected configured workflow and supports `create`, `update`, and
`review` modes. When a review call omits `branch`, the MCP client resolves the ticket's current
open PR branch through Quill before submitting the run; it never substitutes the caller's local
branch for the PR head. There is no duplicate-run bypass; a second submission is rejected while
Quill is occupied. Every run starts from an empty prefix cache, then preserves new prefixes across
phases for the remainder of that run.

Use `quill_status` for current progress. `quill_run_breakdown(run_id)` returns a compact
`phase_executions` array in actual call order. Each entry includes its per-phase call number,
initial/retry flag, verdict/rejection reason, duration, the final transcript context size, token
usage, and tool counts by name. `context_window_tokens` is the latest occupied context per logical
Pi session, so a same-session self-check does not duplicate its parent prompt. Run-level
`cumulative_usage` retains processed usage across phases. It deliberately omits raw transcripts,
tool arguments, and tool results so an MCP
client can consume the complete response without output truncation. That order comes from the
run's append-only `state.jsonl`, which is flushed and filesystem-synced after every queued,
phase, retry, verdict, decision, and terminal transition. Breakdowns therefore remain current
during execution and retain the last complete event if the service or machine stops unexpectedly.

Runs created before durable state history are handled explicitly: surviving transcripts appear as
unordered `legacy_session_observations`, never as invented phase executions or retry counts.

### Update mode (`--update`)

`--update` selects the configured `pr_update` workflow. Quill requires exactly one open PR linked
to the ticket and captures its GitHub head SHA plus latest commit `committedDate`. Only top-level
comments, review summaries, inline comments, replies, or edits whose actionable timestamp is
strictly newer than that boundary enter the run. No qualifying feedback fails before model load.
The compact scope → implementation → focused review → test → build → push → CI flow updates the
same branch and PR. A deterministic pre-push guard halts if another actor moves the PR head during
the run. `pr-feedback.json` and `.md` preserve the exact input and boundary for auditability.

### Run artifacts

Each run's artifacts, findings, logs, contracts, and state land in `~/.quill/runs/<timestamp>-ticketN/` —
one folder per run, never clobbering each other; prune them yourself. Every run unloads all
models on exit. `state.jsonl` is the authoritative ordered workflow history; `stream-*.jsonl`
files contain per-agent usage and tool telemetry, with a unique file for every invocation.
The dashboard lists top-level and nested immutable artifacts with relative paths, size, and a direct download action. It does
not render or copy artifact contents. **Download all** streams a ZIP containing the run's files;
archives spill to a temporary file after 8 MiB instead of retaining the complete ZIP in API memory.

For Pi-backed vLLM runs, Quill loads its bundled `pi_extensions/vllm_live_usage.mjs` extension.
The extension requests vLLM's continuous per-request usage on every streaming model call; Pi's
subagent children inherit the same extension, and Quill replaces each child's latest snapshot
rather than adding repeated progress events. Parent and child input/output therefore contribute
to the live phase and run counters while they execute, then to the persisted phase breakdown. The
existing JSON event stream carries exact prompt/output totals and tool starts into the API.
The dashboard keeps each phase's latest context and accumulates generated output across tool turns
and phases without reading
server-wide Prometheus counters. Pi 0.82.1 or newer is required for this extension hook.

## Layout

```
quill/        driver package (loader, config, engine, mechanical, phases, personas,
              git_ops, pipeline, runstate_file, bootstrap, cli)
quill/pi_extensions/  bundled Pi provider hooks loaded only by Quill-owned workers
quill/_init_assets/   default quillfolio.toml + persona .md files (copied by --init)
quill_api/    FastAPI service (routers, state, events, persistence, runner, packaged web SPA)
quill_mcp/    bounded stdio MCP adapter over the HTTP API
tests/        unit tests
tests/web/    dependency-free browser-module tests
```

## Documentation

Full docs live in the **[project wiki](https://github.com/enisbukalo/quill/wiki)**:

- **[Home](https://github.com/enisbukalo/quill/wiki/Home)** — overview + index
- **[Architecture](https://github.com/enisbukalo/quill/wiki/Architecture)** — execution graph,
  evidence edges, engine/API boundaries, persistence, restart, and security model
- **[API Reference](https://github.com/enisbukalo/quill/wiki/API-Reference)** — the
  `quill-api` HTTP surface
- **[Contributing](https://github.com/enisbukalo/quill/wiki/Contributing)** — dev setup,
  tests, and the CI gates a PR must pass

## Status

Code-complete and exercised through live ticket, update, review, repair, merge, and CI workflows.
CI gates lint, format, types, and tests:

- **Graph engine** — independently validated named workflows; producer, reviewer, finalizer, and
  mechanical nodes; explicit evidence inputs; capacity-aware producer and audit fan-out;
  synthesis; structured findings; convergent gates; selective lane repair; bounded self-fix and
  self-check; verified blocker memory; and checkpoint-backed restart. `run_pipeline` serves both
  the local CLI and API without coupling the driver to the web stack.
- **API** — multi-repository `quill_api` service with persistent server checkouts, strict single-run
  admission, append-only event history, SSE, SQLite projections and lifetime accounting, restart
  reconciliation, durable FIFO project-ticket scheduling, PR feedback cycles, server-side
  persona/skill catalogs, model controls, and the full human + developer-agent endpoint surface.
- **Web control center** — packaged dark synthwave SPA with live Run Pulse, queue/run inspection,
  declared and traversed workflow graphs, breakdowns, artifacts, restart and stop/decision
  controls, project-ticket queue, workspace and verified-memory management, hardware/model
  telemetry, and persona/skill editing. Run creation is available through the dashboard, CLI,
  MCP, and API.

See [`docs/setup/server.md`](docs/setup/server.md) for installation, deployment, and operational
verification.

## License

MIT — see [`LICENSE`](LICENSE).
