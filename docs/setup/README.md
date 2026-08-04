# Machine-side setup

Before quill can drive a pipeline, three things need to be running on your machine:

1. **A model server** — llama.cpp router or vLLM, serving your models over HTTP.
2. **A coding-agent CLI** — [`pi`](https://pi.dev) or [`opencode`](https://opencode.ai), configured to talk to your model server.
3. **GitHub CLI** — `gh`, authenticated with `gh auth login`.

quill ties them together: it invokes the coding-agent CLI for each phase, and the CLI talks to
your model server. quill never talks to models directly.

## 1. Model server

### llama.cpp router (default)

Start the llama.cpp router on `http://localhost:8001` with your models loaded as presets.
Each preset maps to a `model` name in `quillfolio.toml` phases. Sampling (temperature, etc.)
lives server-side in `models.ini` — each preset = one temp = one server entry.

See `models.ini.snippet` in this directory for a reference config.

### vLLM (alternative)

Every run requires `VLLM_SERVER_DEV_MODE=1` because Quill establishes a clean run boundary through
`POST /reset_prefix_cache` before its first prompt. Set
`[runner] backend = "vllm"` in `quillfolio.toml`, then set the server base URL only in the
machine environment, for example `QUILL_VLLM_URL=http://vllm.example:8000`. Do not commit machine
addresses to repository config.
Quill checks each phase model against the exact IDs returned by `GET /v1/models`. Configure
`[runner.vllm] command = ["sudo", "systemctl", "start"]` and map those IDs to systemd services
under `[runner.vllm.models]`; Quill switches services as phases change and waits up to the
configured model-load timeout (six minutes by default).

## 2. Coding-agent CLI

quill drives each phase through a coding-agent CLI. Pick one:

### pi

Set `[runner] kind = "pi"` in `quillfolio.toml`. Configure pi's providers to point at your
model server. Skills are triggered as `/skill:<name>` — set them up in pi's config.

### opencode

Set `[runner] kind = "opencode"` in `quillfolio.toml`. Configure opencode's models/providers
to point at your model server. Skills are triggered as `/<name>`.

See `opencode.models.json` and `opencode.permissions.json` in this directory for reference
configs.

## 3. GitHub CLI

```bash
gh auth login
gh auth refresh -s project   # required when Quill reads or updates GitHub Projects
```

quill preflights `gh` and fails fast with a clear message if it's missing or logged out.

## Verify

```bash
# Model server is reachable
curl http://localhost:8001/v1/models    # or your vLLM endpoint

# Coding-agent CLI can reach the models
pi models                                 # or: opencode models llamacpp

# quill can bootstrap a repo
cd /path/to/repo
quill --init
# edit quillvault/quillfolio.toml: set runner.kind, build.command, build.test
quill 1
```

## Reference files in this directory

| File | Purpose |
|---|---|
| `models.ini.snippet` | Example llama.cpp router presets |
| `opencode.models.json` | Example opencode model config |
| `opencode.permissions.json` | Example opencode permissions |
| `AGENTS.global.md` | Example global opencode agent config |
| `AGENTS.workbench.md` | Example repo-level AGENTS.md |
| `Workbench.quillfolio.toml` | Reference filled-in quillfolio.toml |

> **Phases + personas are repo-level, not machine-level.** Each repo's pipeline lives in
> `quillvault/quillfolio.toml` plus persona `.md` files under `quillvault/personas/`, created
> by `quill --init`. quill owns the entire prompt (persona + skills + task) and injects the
> run dir — it does not rely on the coding CLI's agent abstraction.
