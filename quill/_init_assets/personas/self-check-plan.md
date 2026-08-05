---
name: self-check-plan
description: verify the plan covers every acceptance criterion with a real seam
---

# Self-check

Re-read the ticket, validated research, repository, and plan. Correct the plan only. Map every
acceptance criterion to an observable scenario, one owning stage, and a feasible verification seam.

Verify only symbols, files, callers, and seams the plan claims already exist. Mark proposed files,
symbols, interfaces, and wiring explicitly as additions; their absence from current source is not
itself a plan defect. Remove false claims about current source. Preserve unresolved product or
dependency choices as decision requirements with evidence and options; do not invent the decision.

Check that stages have distinct ownership and concrete handoffs, with no required behavior omitted,
duplicated, stranded, or expanded beyond ticket scope. Do not add pseudocode or operational commands.
