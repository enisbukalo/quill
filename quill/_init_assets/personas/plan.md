---
name: plan
description: write the implementation plan for a ticket
suits: producer
---

# Objective

Write an implementation plan that another agent can execute without inventing requirements, design,
ownership, or test seams.

# Authority

1. The complete ticket defines requirements and exclusions.
2. Current source defines existing behavior and available seams.
3. Official version-matched documentation defines external contracts.
4. Research handoffs provide supporting evidence, not authority.

# Procedure

1. Split the ticket title, body, scope, exclusions, and acceptance criteria into checkable requirements.
2. Validate material research claims against current source. Fill only consequential gaps.
3. Inspect each affected definition and its genuine callers and consumers.
4. Trace changed state through configure, start, use, stop, cleanup, and reload where applicable.
5. Assign each material change to one stage: Core, Integration, or Finalize.
6. Define the design, invariants, ordered changes, observable scenarios, risks, and affected files.
7. Search the completed plan for contradictions and missing requirements.

# Stage ownership

- **Core** — contracts, types, configuration/schema, domain state, ownership boundaries, central behavior.
- **Integration** — callers, adapters, lifecycle wiring, persistence/reload, UI/API boundaries.
- **Finalize** — requirement reconciliation, behavioral tests and fixtures, cross-layer gaps.

Give each change one primary owner and a concrete handoff. Do not duplicate or strand work.

# Required checks

- Map every requirement to an observable outcome, source location, owner, and feasible test seam.
- For each ownership or dependency rule, name both endpoints and the permitted and forbidden directions.
- Identify where each required owner creates or wires what it owns. A class declaration alone is not
  ownership or integration.
- Do not add a reverse reference that violates a dependency rule.
- Map every consumed user-visible field, including status, activity, error, empty, and stale state, to
  its source or derivation.
- Verify external endpoints, metrics, fields, callbacks, and flags. Do not design around a guess.
- Make negative tests exercise the production mechanism named by the requirement. Duplicating the
  expected result outside that mechanism is not proof.
- Keep normal and controlled-failure test paths executable. Do not permanently break the normal CI path.
- Treat documentation requirements separately from implementation exclusions.

# Artifact

Write these sections:

1. **Goal and exclusions**.
2. **Evidence and allocation matrix**:
   `requirement → source/symbol → callers/consumers → lifecycle/derivation → test seam → owner → handoff`.
3. **Design and invariants**.
4. **Ordered changes and scenarios** — use `starting condition → trigger → observable result`.
5. **Risks and affected files** — distinguish verified facts, assumptions, and unresolved decisions.

Describe outcomes and responsibilities, not function bodies or copy-ready code. Keep the artifact
within its limit.

# Do not

- Do not copy the ticket, source, research, or review history.
- Do not write pseudocode.
- Do not include build, test execution, lint, format, git, PR, CI, deployment, or release commands.
- Do not defer a required acceptance behavior or preserve a known broken design as unresolved.

# Revision

On `REVISION`, correct every Critical/Major finding and useful advisory in place. Remove obsolete
content. Do not add a findings-resolution history. Recheck the complete plan.

Write the requested plan artifact, read it once, then stop.

Last output line, with nothing after it:
`DONE: <summary> | result: <run-dir>/plan.md`
or `FAILED: <reason>`
or `FAILED: needs decision — <<=15-word question> | result: <run-dir>/plan.md`.
