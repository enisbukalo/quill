# Objective

Implement every active PR feedback item on the currently checked-out PR branch.

# Authority

The active feedback and current source define required behavior. The update scope organizes the work
but cannot narrow or replace feedback.

# Procedure

1. Build a private checklist: `feedback item → source behavior → meaningful test`.
2. Read each affected definition, caller, and existing test before editing.
3. Implement the feedback items in dependency order.
4. Preserve correct existing PR behavior and keep the diff bounded.
5. For each defect, inspect genuinely analogous paths for the same failure.
6. Add or update behavioral tests for each changed contract.
7. Reconcile every feedback item against the final diff before writing the handoff.

# Required checks

- Preserve required ownership and dependency directions in both directions.
- Verify concrete creation and wiring where feedback requires ownership or integration.
- Exercise named production paths in tests. Do not replace them with fixtures that directly emit the
  expected result.
- Treat documentation requirements separately from exclusions on implementing the documented feature.
- On retry, fix every named review, test, build, or CI finding and check analogous occurrences.

# Do not

- Do not implement unrelated redesign or cleanup.
- Do not create a branch or PR.
- Do not commit or push.
- Do not run configured mechanical test, build, or CI gates.
- Do not claim mechanical checks passed.

# Artifact

Write a compact handoff mapping every feedback item to changed source and tests. Include decisions and
remaining risks. Do not copy feedback or narrate tool use.

End with exactly one receipt line:
`DONE: implemented PR update; result: <absolute artifact path>`
