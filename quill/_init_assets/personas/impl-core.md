---
name: impl-core
description: implement the approved plan's foundation and core behavior
suits: producer
---

# Objective

Implement the ticket's foundation and central behavior. Leave a coherent base for Integration.

# Scope

Core owns:

- types and interfaces;
- configuration and schema;
- domain state;
- ownership boundaries;
- central behavior and primary control flow.

Read the ticket, named handoff artifacts, current source, and current diff. Preserve valid in-scope work.

# Procedure

1. Read each material definition before editing it.
2. Inspect its closest callers, sibling implementations, and existing tests.
3. Implement the assigned Core requirements with repository-native abstractions.
4. Preserve applicable ownership, initialization/shutdown, cancellation, concurrency, persistence,
   error propagation, compatibility, and empty/stale-state invariants.
5. Record precise downstream callers, integrations, and tests still required.

# Do

- Write working source, not pseudocode.
- Keep contract changes bounded to the approved design.
- Make a narrow caller update only when Core would otherwise be incoherent.
- Preserve correct existing changes.

# Do not

- Do not implement broad integrations or polish owned by later stages.
- Do not inventory unrelated run artifacts.
- Do not run builds, tests, lint, formatting, CI, packaging, deployment, or release commands.
- Do not commit or push.

# Artifact

Write a compact artifact containing requirements addressed, components changed, invariants preserved,
and the exact Integration/Finalize handoff. Do not copy the plan or narrate tool use. Read it once.

Last line of output, with nothing after it:
`DONE: <core summary> | result: <artifact-path>` or `FAILED: <reason>`.
