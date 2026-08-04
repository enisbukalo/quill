---
name: impl
description: implement the approved plan
suits: producer
---

# Objective

Implement the complete ticket from the approved plan and current source. Ticket requirements outrank
plan guidance. Current source defines implementation truth.

# Procedure

1. Build a private checklist: `requirement/finding → source behavior → meaningful test`.
2. Read each material symbol before editing.
3. Implement in dependency order: contracts/configuration, core behavior/lifecycle, integrations/callers,
   then tests/fixtures.
4. For each shared contract change, inspect analogous callers, implementations, initializers, mocks,
   persistence paths, and cleanup paths.
5. For each returned finding, fix the root cause and inspect genuinely similar occurrences.
6. Audit the scoped diff once for checklist coverage, missed consumers, accidental files, and scope.

# Required behavior

Preserve applicable lifecycle symmetry, cancellation/cleanup, concurrency, persistence/reload,
failure/recovery, compatibility, and user-visible state. Use repository-native, version-verified APIs.
Record unresolved external uncertainty instead of guessing.

# Do not

- Do not perform unrelated cleanup.
- Do not run builds, tests, lint, formatting, CI, packaging, deployment, or release commands.
- Do not run git commit, push, or PR commands.
- Do not claim checks passed.
- Do not leave the target repository except for named run artifacts and skill-authorized resources.

# Artifact

Write the requested `impl.md` with changed components, important decisions, tests added but not run,
findings resolved, and remaining risk. Read it once, then stop.

Last output line, with nothing after it:
`DONE: <summary> | result: <run-dir>/impl.md`
or `FAILED: <reason>`
or `FAILED: needs decision — <<=15-word question> | result: <run-dir>/impl.md`.
