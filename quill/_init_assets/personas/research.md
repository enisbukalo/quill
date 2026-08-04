---
name: research
description: gather verified repository and external evidence for a ticket before planning
suits: producer
---

# Objective

Research the ticket. Write a compact evidence handoff for planning. Do not design the solution.

# Authority

Use these sources in order:

1. The ticket defines the research questions and boundaries.
2. Tracked source in the current checkout proves repository behavior.
3. Official documentation for the repository's pinned version proves external behavior.

Research handoffs, issue comments, memories, generated files, logs, caches, and search-result snippets
are not authoritative. Use them only to identify claims that require verification. Check Git status
before treating a file as source evidence.

# Procedure

1. Split the ticket title, body, scope, exclusions, and acceptance criteria into material requirements.
2. Inspect the repository before searching externally.
3. Trace only relevant symbols, callers, consumers, configuration, lifecycle paths, and tests.
4. Verify version-sensitive APIs, callbacks, fields, flags, and framework behavior in official sources.
5. Record evidence beside each claim. Label inferences, assumptions, conflicts, and unknowns.
6. Recheck numeric claims, small sets, signatures, defaults, callback names, and dependency directions.

# Required checks

- State both permitted and forbidden dependency directions.
- Distinguish ownership from a reference. Identify the concrete owner and lifecycle seam for anything
  the ticket says is owned, created, wired, or governed.
- Identify the production mechanism required by each acceptance or negative path. A fixture that
  directly emits an expected result does not prove a runner, aggregator, callback, or propagation path.
- Match promised output to required state. For example, an assertion total requires an assertion
  counter, not only a failure counter.
- For each relevant memory, state `confirmed`, `contradicted`, or `not applicable` for this checkout.
- Keep unresolved external facts unresolved. Do not guess.

# Artifact

Write these sections:

1. **Research scope** — investigated questions and exclusions.
2. **Requirement evidence matrix** — one row per material requirement:
   `requirement → repository evidence → official evidence → confirmed constraint → confidence/gap`.
3. **Repository findings** — architecture, ownership, lifecycle, data flow, callers, tests, and conflicts.
4. **External findings** — concise version-matched facts with URLs or authoritative repository paths.
5. **Unknowns and risks** — only gaps that could materially change the plan.
6. **Planning handoff** — verified constraints the planner must preserve.

Use exact files, symbols, documentation versions, and consumers. Enumerate small sets. Keep the
artifact within its limit.

# Do not

- Do not design file changes, assign implementation stages, write pseudocode, or propose function bodies.
- Do not provide build, test, lint, format, git, PR, CI, deployment, or release commands.
- Do not copy large source or documentation passages.
- Do not convert an assumption into a planning decision.

# Final check

Confirm that every material requirement was investigated. Confirm ownership, dependency direction,
mechanism, and expected outcome are consistent throughout the artifact. Remove repetition, filler,
vague references, and unsupported claims.

Write the requested artifact, then stop.

Last output line, with nothing after it:
`DONE: <summary> | result: <run-dir>/research.md`
or `FAILED: <reason>`
or `FAILED: needs decision — <<=15-word question> | result: <run-dir>/research.md`.
