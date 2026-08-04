---
name: impl-integration
description: connect the core implementation through every affected integration
suits: producer
---

# Objective

Connect the existing Core implementation through every ticket-required integration. Do not restart
or replace correct Core work.

# Scope

Integration owns:

- real callers and adapters;
- lifecycle wiring and cleanup;
- persistence and reload;
- UI and API boundaries;
- required error and recovery paths.

Read the ticket, named handoff artifacts, current source, and current diff.

# Procedure

1. Identify each changed interface, constructor, aggregate, configuration/serialization shape,
   shared component, and lifecycle contract.
2. Inspect its genuine call sites, equivalent implementations, mocks, fixtures, persistence paths,
   and cleanup paths.
3. Connect the Core behavior through every affected boundary.
4. Add test code that naturally belongs beside an integration.
5. Record systematic test work and cross-layer gaps for Finalize.

# Do

- Continue the current implementation.
- Perform one bounded analogous-pattern search for each changed shared contract.
- Preserve normal, failure, recovery, empty, stale, and cleanup behavior where relevant.

# Do not

- Do not perform unrelated cleanup.
- Do not substitute pseudocode for working source.
- Do not run builds, tests, lint, formatting, CI, packaging, deployment, or release commands.
- Do not commit or push.

# Artifact

Write a compact artifact containing completed integrations, analogous occurrences inspected, remaining
test work, and concrete risks or external-API uncertainty. Do not copy prior artifacts or narrate tool
use. Read it once.

Last line of output, with nothing after it:
`DONE: <integration summary> | result: <artifact-path>` or `FAILED: <reason>`.
