# Workbench AGENTS.md (project-specific conventions)

> WI-6: the Workbench-specific conventions moved out of the global AGENTS.md (WI-7). Place at
> `~/Documents/GitHub/Workbench/AGENTS.md`. Fill the bracketed bits from the real Workbench
> conventions you already follow — the headings are the contract; the specifics are yours.

## Build & test

- Build: `[build.sh invocation]` — confirm the exact flags.
- Test: `[build.sh -t / test target]` — confirm the test target.
- Logs land in `logs/` (the phase-5 gate reads the captured `test-log.txt`).

## CMake

- **Globbing:** `[the project's CMake globbing rule]`.
- **Explicit test list:** tests are listed explicitly in `tests/CMakeLists.txt` — `[the
  convention for adding a test]`.

## Code conventions

- **Config-struct patterns:** `[the project's config-struct convention]`.
- **Build-edit rule:** `[when/how edits must be reflected in the build files]`.

## Project board

- Board: `[Workbench project board name / facts]` (the driver's phase-6 board step uses this;
  mirrored in `quillvault/quillfolio.toml` → `[repo].project_board`).
