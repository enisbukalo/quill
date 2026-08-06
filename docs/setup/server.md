# Running quill as a service

quill can run as an always-on service on one machine. Any client names a repository, branch, and
ticket and the server does the rest — no GPU, model server, agent CLI, or `gh` auth needed on the
client.

This works because quill's unit of work is a **GitHub ticket, not a working tree**. Every mutation
is performed by agent phases through `git`/`gh` inside the server's own checkout, and the driver
only *reads* the ticket body and PR feedback. The client sends `{repo, branch, ticket, mode}`; the
server materialises everything else and loads the committed root `quillfolio.toml`.

## Fresh-machine checklist

Follow these steps in order. Quill creates its state directories and database when `quill-api`
starts, but it does not install or configure the external tools that execute a run.

1. **Install host tools.** Install Python 3.12 or newer, `uv`, `git`, and `gh`. Install the build and
   test dependencies required by every repository this server will execute.
2. **Install Quill.**

   ```bash
   uv tool install "quill[api,mcp] @ git+https://github.com/enisbukalo/quill@main"
   command -v quill quill-api quill-mcp
   quill --help
   ```

3. **Authenticate GitHub as the service user.**

   ```bash
   gh auth login
   gh auth refresh -s project
   gh auth status
   ```

4. **Install and start the model server.** Configure vLLM or the llama.cpp router named by each
   repository's `quillfolio.toml`. Quill detects model-server failures but never installs or starts
   an unconfigured server.
5. **Install and configure the coding-agent CLI.** Install Pi or OpenCode, configure its provider to
   use the model server, and run one direct prompt successfully before involving Quill.
6. **Provide persona and skill libraries.** Follow [Persona and skill libraries](#2-persona-and-skill-libraries).
   `quill --init` seeds packaged personas only when `~/.quill/personas` is empty. It does not install
   skills.
7. **Create the environment file.** Set the paths and network values under [Environment](#3-environment).
   Replace every example username, address, and path with a value valid on this host.
8. **Deploy the dashboard.** Run `./scripts/deploy-web.sh` when using an external
   `QUILL_WEB_ROOT`. The packaged dashboard needs no separate deployment.
9. **Install and start the user service.** Follow [systemd](#4-systemd), including
   `loginctl enable-linger "$USER"`.
10. **Initialize each target repository.** From the repository root, run `quill --init`, configure
    `quillfolio.toml`, and commit it to the repository's default branch. Server discovery lists only
    repositories whose remote default branch contains a root `quillfolio.toml`.
11. **Verify readiness.**

    ```bash
    command -v quill quill-api quill-mcp gh
    command -v pi       # or: command -v opencode
    gh auth status
    curl -fsS http://localhost:8002/health | jq
    curl -fsS http://localhost:8002/models | jq
    curl -fsS http://localhost:8002/personas | jq '.entries[].name'
    curl -fsS http://localhost:8002/skills | jq '.entries[].name'
    curl -fsS http://localhost:8002/queue | jq
    ```

    Stop here if `/health` is not up, `/models` is unreachable, required personas or skills are
    missing, or `gh auth status` fails.
12. **Run one disposable ticket end to end.** Confirm that Quill creates its workspace, executes the
    configured phases, opens or updates the PR, and reports the final run in the dashboard.

### What Quill creates automatically

The first `quill-api` start creates `$QUILL_STATE_DIR`, `$QUILL_WORKSPACE_ROOT`, and
`$QUILL_RUNS_ROOT`, including `quill.db` and the repository-discovery cache. Later runs create
repository workspaces, run directories, telemetry projections, and memory files as needed.

Quill does not automatically create a production environment file, systemd unit, firewall rules,
persona content, skills, `quillfolio.toml`, model server, coding-agent CLI, GitHub authentication,
or repository build toolchain.

## What lives where

| Thing | Where | Notes |
|---|---|---|
| `quillfolio.toml` | root of the target repo | Read from the server checkout. Missing or invalid config fails the run visibly. |
| personas | `$QUILL_PERSONAS_DIR` | One library shared by every repo. Usually a symlink into a config repo. |
| skills | `$QUILL_SKILLS_DIR` | Same. Defaults to `~/.pi/agent/skills` so the service sees what `pi` would. |
| checkouts | `$QUILL_WORKSPACE_ROOT` | One persistent clone per repo. Build caches survive between runs. |
| run artifacts and durable state | `$QUILL_RUNS_ROOT/<run-id>` | Artifacts, unique agent transcripts, and append-only `state.jsonl`, outside the checkout. |
| run history | `$QUILL_STATE_DIR/quill.db` | SQLite. Survives restarts. |

## 1. Machine prerequisites

The server needs the full stack — this is the machine that actually does the work:

1. A **model server** (llama.cpp router or vLLM). See the [README](../../README.md).
2. A **coding-agent CLI** (`pi` or `opencode`), configured to reach it.
3. **`gh`**, authenticated: `gh auth login`.

Clients need none of this.

## 2. Persona and skill libraries

Both are plain directories. Keeping them in a git repo means edits made over the API are committed
and pushed automatically, so `git log` records what changed and why:

```bash
git clone <your-configs-repo> ~/Documents/GitHub/configs
mkdir -p ~/.quill
ln -s ~/Documents/GitHub/configs/opencode/skills   ~/.quill/skills
ln -s ~/Documents/GitHub/configs/quill/personas    ~/.quill/personas
```

Seed the persona library from quill's defaults if it is empty:

```bash
quill --init      # in any repo: writes quillfolio.toml AND seeds the persona library
```

Check what the server can see:

```bash
curl -s localhost:8002/personas | jq '.entries[].name'
curl -s localhost:8002/skills   | jq '.entries[].name'
```

A config naming a persona the server does not have fails validation up front, with the missing
name in the message — it never silently runs a phase without one.

## 3. Environment

Every machine-specific address, port, and path lives in this one file. Nothing here belongs in
source control or in a repository's `quillfolio.toml`.

```ini
# ~/.config/quill/quill.env
QUILL_HOST=0.0.0.0
QUILL_PORT=8002
QUILL_STATE_DIR=/home/YOU/.quill
QUILL_WORKSPACE_ROOT=/home/YOU/.quill/workspaces
QUILL_RUNS_ROOT=/home/YOU/.quill/runs
QUILL_PERSONAS_DIR=/home/YOU/.quill/personas
QUILL_SKILLS_DIR=/home/YOU/.pi/agent/skills
# Model-server base URLs. Set the one your repositories' `[runner] backend` selects; set both if
# different repositories use different backends. Prefer a loopback address when the model server
# runs on this same host — routing through a public address hairpins through the router and
# needlessly exposes the port.
QUILL_VLLM_URL=http://127.0.0.1:8000
QUILL_ROUTER_URL=http://127.0.0.1:8001
# Authorship stamped on every agent-made commit, set repo-locally on each checkout. Without it a
# checkout inherits the service user's global git identity and publishes a personal address on
# machine-generated commits. Use a dedicated automation account's GitHub noreply address, which
# attributes the commit to that account without exposing a real mailbox. Find the numeric ID with
# `gh api users/<login> --jq .id`.
QUILL_GIT_AUTHOR_NAME=your-bot-account
QUILL_GIT_AUTHOR_EMAIL=00000000+your-bot-account@users.noreply.github.com
# Token the service authenticates as. `gh` prefers this over any stored login, and `git push`
# reaches it through gh's credential helper, so this one value covers clones, pushes, and PRs.
# A classic token with `repo` + `project` scopes; the account needs collaborator access on each
# target repository and membership of the project board. Keep this file mode 600.
GH_TOKEN=
# External, atomically deployed dashboard assets. This decouples frontend updates from API restarts.
QUILL_WEB_ROOT=/home/YOU/.local/share/quill/web/current
# Backend CPU/GPU sampling cadence (8 samples/second by default).
QUILL_TELEMETRY_INTERVAL_SECONDS=0.125
# Optional CPU fan command gauge. Use the stable hwmon driver name and PWM channel, not the
# boot-dependent /sys/class/hwmon/hwmonN directory number.
QUILL_CPU_FAN_HWMON_NAME=nct6779
QUILL_CPU_FAN_PWM_CHANNEL=3
# Machine-level vLLM controls used by the Models page. The service user needs passwordless sudo
# for start and stop on each discovered model unit.
QUILL_VLLM_SWITCH_COMMAND=sudo systemctl start
QUILL_VLLM_STOP_COMMAND=sudo systemctl stop
# Poll configured repositories for completed PR check rollups. Defaults: enabled, 15 seconds.
QUILL_PR_WATCH_ENABLED=true
QUILL_PR_WATCH_INTERVAL_SECONDS=15
# Turn validated BLOCK reviews into SHA-bound pr_update runs. Defaults: enabled, 5 cycles.
QUILL_PR_FEEDBACK_LOOP_ENABLED=true
QUILL_PR_FEEDBACK_LOOP_MAX_CYCLES=5
# Stabilize GitHub Project Queue snapshots before admitting a ticket. Defaults: enabled, 5 seconds.
QUILL_PROJECT_QUEUE_WATCH_ENABLED=true
QUILL_PROJECT_QUEUE_WATCH_INTERVAL_SECONDS=5
```

Automatic PR review requires a `pr_review` workflow in the repository's default-branch
`quillfolio.toml` and a non-draft PR linked to exactly one closing issue. The default policy also
requires a nonempty check rollup whose checks all completed successfully. Repositories that run
Quill's local verification without PR Actions may set `[repo].pr_checks_required = false`; an empty
rollup then becomes eligible, while malformed data and any reported failed, canceled, timed-out,
skipped, neutral, or pending check remain ineligible. The ticket workflow's mechanical `ci_check`
verifies GitHub's parsed closing-issue metadata
before waiting on CI. When the selected ticket is absent, Quill appends `Closes #<ticket>` to the
existing PR body and verifies the resolved link; an unrepairable link fails the run. Quill queues one
review per head SHA after its check policy is satisfied. Set `QUILL_PR_WATCH_ENABLED=false` to require manual
review runs only.

With `QUILL_PR_FEEDBACK_LOOP_ENABLED=true`, a validated `BLOCK` comment queues one `pr_update`
against the reviewed head. A successful update must push a new head, and the watcher queues the
next review once it satisfies the repository check policy. A validated `PASS` rechecks the exact
reviewed head, branch, base, clean merge state, and check policy before creating a merge commit and
deleting only the remote feature branch. The local workspace branch remains intact. A stale or unchanged head,
update failure, and the configured cycle limit stop automatic progression. The cycle outbox is
stored in Quill's SQLite database, so a service restart cannot dispatch the same reviewed head
twice; one interrupted dispatch may be replayed once. Set
`QUILL_PR_FEEDBACK_LOOP_ENABLED=false` to retain automatic review without automatic remediation.

Deploy the dashboard before the first service start, and whenever frontend files change:

```bash
./scripts/deploy-web.sh
```

The script copies a versioned asset set and atomically moves the `current` symlink. Once
`QUILL_WEB_ROOT` is configured, frontend-only deployments require a browser refresh but do not
reinstall Quill or restart `quill-api`. Python/API changes still require the normal package install
and service restart.

The dashboard Settings page stores CPU and GPU temperature-bar ranges in Quill's SQLite history
database through `GET /settings/telemetry` and `PUT /settings/telemetry`. Defaults are 20–70°C for
CPU and 20–80°C for GPU. RAM and VRAM bars derive their ranges from reported hardware capacity.

The Models page can load a discovered vLLM service or unload the resident service. Loading runs
`$QUILL_VLLM_SWITCH_COMMAND <unit>`; unloading runs `$QUILL_VLLM_STOP_COMMAND <unit>` and waits
until the model is no longer advertised. Both operations reject active or queued runs unless the
operator explicitly confirms the forced action. Configure passwordless sudo for the exact model
unit start and stop commands so the API never waits for an interactive password.

## 4. systemd

A **user** service, so it runs as you and inherits your `gh` auth:

```ini
# ~/.config/systemd/user/quill-api.service
[Unit]
Description=quill API
# After, not Requires: the API must stay up to REPORT a down GPU stack rather than
# refuse to start alongside it.
After=network-online.target

[Service]
ExecStart=%h/.local/bin/quill-api
EnvironmentFile=%h/.config/quill/quill.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
loginctl enable-linger "$USER"     # survive logout
systemctl --user daemon-reload
systemctl --user enable --now quill-api
curl -s localhost:8002/health | jq
```

`enable-linger` is not optional: without it systemd stops your user services when you log out, and
the "always-on" service is only on while you are.

## 5. Web control center

The API process also serves Quill's single-page dashboard at its root:

```text
http://quill-box:8002/
```

It provides run submission, health/model discovery, the live queue, filtered and paginated run
history, status and phase breakdowns, artifact downloads and ZIP archives, stop/decision controls, and full persona/skill
catalog editing. Submission uses the configuration committed in the server-side checkout.
Repository and open-ticket choices come from the server's authenticated `gh` account; branch work
types are inferred from issue labels and recent PR naming conventions.

The animated Run Pulse reflects real queue/run states and incoming SSE events, not a fabricated
completion percentage. Its resource gauges show Linux CPU and NVIDIA GPU load and temperature from
the backend. `/events` pushes live run/queue projections; `/telemetry/events` pushes ephemeral
host samples every 0.125 seconds. REST is used for startup, details, and bounded reconnect recovery,
not routine polling. Browsers requesting reduced motion receive the same live values without
continuous decorative animation.

For a Pi-backed `vllm` run, token counters come from the exact OpenAI-compatible response stream.
`PiRunner` loads Quill's bundled `vllm_live_usage.mjs` extension, which adds
`stream_options.include_usage=true` and `continuous_usage_stats=true` to each streaming request.
Pi exposes each response chunk and every tool start through its JSON stdout stream. Quill retains
cumulative processed input/output for run accounting, while `context_window_tokens` records the
latest occupied context for each logical Pi session. Same-session repairs and self-checks replace
that session's prior window instead of adding a duplicate snapshot. Phase rows and graph nodes show
the occupied context; run totals continue to show processed usage across phases. The latest values
are retained for SSE reconnect sync and the active Breakdown summary. This requires Pi 0.82.1 or
newer.

Quill does not use vLLM's server-wide Prometheus token counters for run accounting. Providers that
ignore continuous usage still produce their final request usage at completion; other runners retain
their settled-transcript behavior. Missing usage pauses token updates but never fails the pipeline,
and tool-call progress remains live independently.

CPU temperature uses the Linux `Tdie` sensor when available, with package/Tctl and thermal-zone
fallbacks. No fan-control offset is applied. NVIDIA metrics use NVML when available and a slower
`nvidia-smi` fallback. Unsupported or missing sensors are reported as `null`, never as fabricated
values.

The dashboard uses the API's existing network trust boundary. Anyone who can reach it can stop work,
answer decisions, and edit the shared catalogs; the firewall guidance below therefore applies to the
page as well as the JSON endpoints.

## 6. Queue and asynchronous MCP access

`GET /queue` is the authoritative point-in-time occupancy view. `active` is the executing run and
`depth` is at most one through the public API. A POST while any run is queued, running, or awaiting
a decision returns 409; Quill intentionally exposes no duplicate or waiting-queue bypass:

```bash
curl -fsS localhost:8002/queue | jq
```

`GET /queue` is the short-lived admitted-run queue. `GET /project-queue` is the durable GitHub
Project ticket scheduler. Repositories appear on the dashboard Queue page only when their root
config names `[repo].project_board`; the board must have one `Status` field and exact `Queue`,
`In progress`, `In review`, and `Done` options. The service user's token needs the `project` scope
because Quill reads Project items and changes their status.

Each submission is a FIFO batch, including a single-ticket submission. Quill waits for one unchanged
five-second board snapshot, orders tickets numerically within the batch, and admits only the head.
The next ticket remains blocked until the current ticket's exact PR is verified as merged. Failure
or halt pauses the scheduler; restarting the API neither skips nor duplicates the owned ticket.

Install the MCP extra on machines running an MCP client and configure `QUILL_SERVER` to reach this
API. `quill-mcp` communicates over stdio with the client and uses ordinary bounded HTTP requests to
Quill. Its start call returns immediately; users return later and call status/recent/queue.

```bash
uv tool install "quill[mcp] @ git+https://github.com/enisbukalo/quill@main"
QUILL_SERVER=http://quill-host.example:8002 quill-mcp
```

It exposes `quill_start`, `quill_status`, `quill_run_breakdown`, `quill_recent_runs`,
`quill_queue`, `quill_stop`, and `quill_answer`. It deliberately provides no wait or streaming
operation. Start calls support create, update, and pull-request-review modes. A review start with
no explicit branch resolves the ticket's current open PR branch through the API before submission.

`GET /runs/{run_id}/breakdown` and `quill_run_breakdown` return a compact chronological
`phase_executions` array. Every actual phase invocation gets its own entry—including revisions and
verification passes—with call number, verdict/rejection reason, duration, context and token usage,
and tool counts by name. `context_tokens` is the final valid transcript snapshot,
while top-level `cumulative_usage` adds actual phase usage once. Raw transcripts and individual tool payloads remain on disk
and are not returned through MCP.

The server appends every lifecycle transition to `$QUILL_RUNS_ROOT/<run-id>/state.jsonl` before
publishing it or updating in-memory state. Every append is followed by `flush()` and `fsync()`, so
the file is queryable while a run is active and remains the authoritative sequence after a crash.
It starts at `run_queued` and records phase starts/completions, gate verdicts, retries,
needs-decision transitions, and terminal outcomes. The breakdown endpoint reconstructs execution
order from this file and joins each phase with its live `stream-*.jsonl` statistics.

When a run halts or fails, the server treats its checkout as disposable: it hard-discards tracked
changes, removes untracked files (while retaining ignored build caches), checks out and resets
`main` from `origin/main`, and force-deletes the failed run's **local** branch. The corresponding
remote branch is never deleted. Successful runs retain their normal checked-out/pushed branch.

## 7. Firewall

The service listens on **8002**. Current rules on this box:

```
[ 1] 22/tcp        ALLOW IN   Anywhere
[ 2] Samba         ALLOW IN   192.168.0.0/16
[ 3] Anywhere      ALLOW IN   192.168.0.0/16
[ 4] 8000          ALLOW IN   192.0.2.10
[ 5] 22/tcp (v6)   ALLOW IN   Anywhere (v6)
```

Rule 3 already allows **every** port from the LAN, so 8002 is reachable there the moment it binds.
Adding the rule anyway makes the exposure explicit rather than an accident of a broad rule, then
grant the same external host that already reaches vLLM on its configured port:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8002 proto tcp comment 'quill-api LAN'
sudo ufw allow from 192.0.2.10      to any port 8002 proto tcp comment 'quill-api external'
sudo ufw status numbered
ss -lntp | grep 8002        # confirm what it actually bound to
```

**There is no application-layer auth.** Anything that reaches the port can start runs, stop them,
and rewrite personas and skills. That is deliberate for a single-user service whose reachable set is
one LAN plus one whitelisted host — but it makes `ufw` the entire trust boundary. Re-check
`ufw status numbered` after any firewall change; a later broad rule can widen 8002 the same way
rule 3 already does. If the reachable set ever grows beyond machines you own, auth has to come back.

## 8. Using it from another machine

```bash
cd ~/Documents/GitHub/Workbench
quill 42 --server http://quill-box:8002
```

The client derives `owner/name` from `origin`, uses the current branch (override with `--branch`),
and streams the run back. The server reads `quillfolio.toml` from its checkout. Output is identical
to a local run: the same console renderer consumes the same events, whichever machine produced
them.

Set `QUILL_SERVER=http://quill-box:8002` to drop the flag.

`--resume` and `--start-phase` are local-only — both replay state that lives on the machine that
drove the run.

## 9. Letting CI do the building

The server does **not** need each repo's toolchain if the repo gates on GitHub Actions instead of a
local build. Replace `build_test` with a `ci_check` phase:

```toml
[[phase]]
id           = "ci"
type         = "mechanical"
step         = "ci_check"
gates        = true
retry_budget = 1
on_block     = "impl"
```

It must run *after* the phase that pushes and opens the PR, and needs a workflow triggered by
`on: pull_request`. `on_block = "impl"` is a graph back-edge: Quill jumps to `impl`, then traverses
every configured phase after it—including review, local gates, and commit—before checking CI again.

## 10. Gate ordering and convergence

Two configuration choices decide whether a run finishes or loops, and both matter more with a
smaller local model than with a frontier one.

**Order the mechanical gates before the LLM review.** A `test` or `build` verdict is deterministic,
cheap, and routes back to `impl` with a real log. An LLM review is slow, expensive, and noisy.
Placing `impl -> test -> build -> review -> gate` means reviewers only ever spend rounds on code
that already compiles and passes, and stops them discovering lint failures a linter owns. Because
a retry route is the contiguous slice between `on_block` and the gate, this order also causes every
review revision to be mechanically re-verified before the reviewers see it — no extra configuration.

**Set `retry_blocking = "repeat-only"` under `[gates]`.** Without it, reviewers re-reading revised
code can replace the blockers just fixed with fresh ones of equal severity, and the gate never
converges. See the README's *Converging gates* section for the full table.

Model choice interacts with this. A dense model that follows an output contract reliably is worth
more here than a larger sparse one: a mixture-of-experts model with few active parameters tends to
burn its budget on contract repair rather than on the review itself. Prefer a model that clears
gated phases on the first attempt, and check `state.jsonl` for repeated `self_fix_started` events
as the signal that it does not.

## Troubleshooting

| Symptom | Check |
|---|---|
| `424 gh_not_ready` | `gh auth status` as the service user |
| Run fails with "not in the persona library" | `curl localhost:8002/personas` — the config named something the server lacks |
| Runs queue but never start | `systemctl --user status quill-api`; `curl localhost:8002/queue` |
| `/models` says unreachable | The model server is down. The API stays up so you can see that. |
| Everything `running` after a reboot | Restart reconciliation marks them failed; check `curl localhost:8002/runs` |
