---
name: impl-finalize
description: reconcile the implementation, tests, and returned gate findings
suits: producer
---

# Objective

Complete the implementation and behavioral tests. On retry, fix every evidence-backed blocking
finding from review, local test/build, or CI.

# Authority

Read the complete ticket, named handoff and findings artifacts, current source, and current diff.
Preserve correct prior work. Fix root causes even when they cross Core or Integration boundaries.

# Procedure

1. Build a private checklist:
   `requirement/design decision/accepted finding → source behavior → meaningful test`.
2. Inspect current definitions and callers before editing.
3. Complete missing cross-layer behavior and behavioral tests.
4. For each returned failure, inspect genuinely analogous scenarios for the same defect class.
5. Audit the scoped diff once for missed callers/contracts, lifecycle/cleanup, concurrency,
   persistence/reload, recovery, compatibility, fixtures/mocks, accidental files, and ticket scope.

# Test standard

Tests must assert observable outcomes and meaningful state transitions. Construction and absence of an
exception are insufficient when behavior can be asserted.

# Do not

- Do not replace working source with pseudocode.
- Do not run builds, tests, lint, formatting, CI, packaging, deployment, or release commands.
- Do not commit or push.
- Do not claim mechanical checks passed.

# Artifact

Write the requested `impl.md` with:

- requirements implemented;
- materially changed components;
- important decisions;
- tests added or updated but not run;
- findings resolved and analogous patterns checked;
- remaining risk.

Keep it compact. Do not copy the plan/findings or narrate tool use. Read it once, then stop.

Last line of output, with nothing after it:
`DONE: <summary> | result: <artifact-path>` or `FAILED: <reason>` or
`FAILED: needs decision — <<=15-word question> | result: <artifact-path>`.
