---
name: self-check-findings
description: re-verify each written finding against the source it cites
---

# Self-check

Every finding you just wrote is a claim about source you do not own. Verify each one before it
costs another producer round.

For each finding in your artifact:

1. Open the exact file and line your `evidence` field cites. Read it now — do not rely on what you
   recall reading earlier.
2. If the finding claims something is **absent, uncovered, missing, or only partial** — a test that
   "does not cover", a path "never exercised", a case "not asserted" — read the **entire** function,
   class, or file the claim is about, from its first line to its last. A claim of absence is only
   true if you have seen the whole thing it is absent from.
3. Decide:
   - The source confirms the finding → keep it, with the citation you verified.
   - The source refutes it → set `status` to `RESOLVED` and put the refuting line in `evidence`.
   - You cannot find the cited code at all → remove the finding.

## Rules

- Resolve a finding **only** with a quoted line from current source that refutes it. "Probably
  fixed", "appears addressed", and "the receipt says so" are not evidence.
- Do not add findings. A self-check narrows an existing set; it never grows one.
- Do not change a finding's severity to make it non-blocking. Either the source refutes it or it
  stands.
- Merge two findings only when they name the same root cause, keeping the lower ID.

A finding that survives this pass should be one you could defend by pasting one line of source.
