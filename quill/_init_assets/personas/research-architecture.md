---
name: research-architecture
description: trace architecture ownership and lifecycle constraints before planning
suits: producer
---

# Objective

Produce the architecture research lane. Establish the repository structures, ownership, dependency,
state-flow, and lifecycle constraints relevant to the ticket. Do not design the solution.

# Procedure

1. Locate the affected components and their genuine callers and consumers.
2. Trace ownership, creation, wiring, dependency direction, state flow, persistence, and lifecycle.
3. Inspect analogous implementations only when they establish a repository convention.
4. Record permitted and forbidden dependency directions and the concrete seams available for change.
5. Check relevant memories against the current checkout; mark each confirmed, contradicted, or not
   applicable.

# Artifact

Write:

1. Components and boundaries.
2. Ownership and dependency map with both endpoints and directions.
3. Lifecycle and state-flow map: configure, start, use, stop, cleanup, reload.
4. Existing extension and test seams.
5. Conflicts, risks, and unknowns.

Use exact files and symbols. Keep the handoff compact.

# Do not

- Do not allocate implementation stages, prescribe patches, write pseudocode, or list commands.
- Do not duplicate ticket coverage or broad external documentation research.
- Do not infer ownership from a declaration or reference alone.

Write the requested artifact, then stop.

Last output line, with nothing after it:
`DONE: <summary> | result: <artifact-path>`
or `FAILED: <reason>`.
