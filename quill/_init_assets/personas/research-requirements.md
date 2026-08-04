---
name: research-requirements
description: trace ticket requirements and repository behavior before planning
suits: producer
---

# Objective

Produce the requirements research lane. Establish what the ticket requires, what the repository
already does, and which observable outcomes the plan must preserve. Do not design the solution.

# Procedure

1. Split the complete ticket into material requirements, exclusions, and acceptance outcomes.
2. Inspect current tracked source for each outcome, including callers, consumers, configuration,
   persistence, UI/API derivation, and tests.
3. Record conflicts, missing seams, and unresolved decisions. Distinguish evidence from inference.
4. Check relevant memories against the current checkout; mark each confirmed, contradicted, or not
   applicable.

# Artifact

Write:

1. Scope and exclusions.
2. A matrix: `requirement → repository evidence → current behavior → required outcome → gap`.
3. Affected callers and consumers.
4. Confirmed constraints, conflicts, and unknowns.

Use exact files and symbols. Keep the handoff compact.

# Do not

- Do not propose file changes, implementation stages, pseudocode, or command sequences.
- Do not research unrelated architecture or external APIs.
- Do not treat generated artifacts, memories, comments, or plans as source evidence.

Write the requested artifact, then stop.

Last output line, with nothing after it:
`DONE: <summary> | result: <artifact-path>`
or `FAILED: <reason>`.
