---
name: research-synthesis
description: reconcile current research lanes into one planning handoff
suits: producer
---

# Objective

Synthesize the current requirements, architecture, and technical research artifacts into one compact,
internally consistent planning handoff. Do not perform a fourth broad research pass or design the solution.

# Procedure

1. Read every supplied lane artifact completely.
2. Reconcile duplicate claims, terminology, conflicts, and unknowns against cited evidence.
3. Preserve each material requirement, architecture constraint, external contract, and validation seam.
4. Remove repetition. Keep unresolved conflicts explicit and record every open question
   under **Open decisions** below rather than leaving it implied.
5. On revision, rebuild the synthesis from all current lane artifacts. Replace stale material from rerun
   lanes; do not append a change log.

# Artifact

Write:

1. Research scope and exclusions.
2. Requirement evidence matrix.
3. Architecture, ownership, dependency, lifecycle, and state-flow constraints.
4. Versioned external contracts and validation seams.
5. Confirmed memories, conflicts, risks, and unknowns.
6. Planning handoff: the complete verified constraints the plan must preserve.

Use exact files, symbols, versions, and evidence links. Keep the artifact within its limit.

# Open decisions

Never write that something is "decided" without stating the decision. Every open question ends as
exactly one line, in one of two forms:

- `DECIDED: <choice> — <evidence or documented convention it follows>`
- `MUST DECIDE (owner: plan): <question> — options: <a> | <b> — tradeoff: <one clause>`

Choosing between documented project conventions is a decision, and it is yours to make. Inventing a
class layout, field set, file path, or algorithm is design, and it is the plan's to make.

# Do not

- Do not propose file changes, implementation stages, pseudocode, or command sequences.
- Do not hide disagreements between lanes.
- Do not copy repeated evidence when one precise statement is sufficient.

Write the requested artifact, read it once, then stop.

Last output line, with nothing after it:
`DONE: <summary> | result: <artifact-path>`
or `FAILED: <reason>`.
