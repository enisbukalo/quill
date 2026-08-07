---
name: self-check-findings
description: re-verify each written finding against the source it cites
---

# Self-check

Re-read the natural review notes, the reviewed artifacts, ticket or feedback, and the relevant
source. Correct the review notes only; never modify repository work.

Verify every claimed defect and every claimed resolution with direct, bounded inspection appropriate
to the claim. For absence claims, inspect the complete relevant scope and name that scope. Remove or
correct unsupported claims, merge duplicate root causes, and fix severity based on observable impact.
If the original task supplied prior finding IDs or required outcomes, discuss every one explicitly
and preserve its identity and meaning unless evidence proves it resolved. A completion self-check may
add a genuinely missed in-scope defect; it must not broaden the review target or invent authority.

Escalation is legitimate scope: the research gate may set `escalation_reason` on findings that are
decision-points (multiple options with trade-offs, repeated blocks, ticket language leans one way)
rather than defects. Marking a finding `RESOLVED` with an `escalation_reason` is not overreach —
it is the gate routing a planning-phase decision to the phase that makes design choices.
