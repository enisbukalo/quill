# Objective

Define a bounded, executable scope for updating an existing pull request.

# Authority

1. The `ACTIVE PR FEEDBACK` block defines required changes.
2. Current source and the PR diff define existing behavior.
3. Older discussion is context, not a new requirement.

# Procedure

1. Split every active feedback comment into independently verifiable requirements. When one feedback
   ID contains multiple requirements, assign stable item suffixes such as `F1.1` and `F1.2`.
2. Inspect the current PR diff and relevant source.
3. Map every feedback item to:
   - required observable outcome;
   - current evidence and failure;
   - affected contracts and files;
   - regressions to avoid;
   - production test seam and expected assertion.
4. Identify dependencies and a bounded implementation order.
5. Confirm every active feedback item appears exactly once in the scope.

# Required checks

- Preserve ownership, dependency direction, lifecycle, and production-path requirements exactly.
- Treat documentation requirements separately from implementation exclusions.
- A declaration or documentation claim does not prove runtime wiring.
- A test proxy does not prove a named production runner, aggregator, callback, or propagation path.
- Preserve correct existing PR work.

# Do not

- Do not redesign unaffected areas.
- Do not invent requirements from older discussion.
- Do not write pseudocode or implement changes.
- Do not commit, push, or run mechanical gates.

# Artifact

Write a compact scope with a feedback coverage matrix, ordered changes, test evidence, and bounded
risks. Another agent must be able to implement every item without interpreting vague prose.

End with exactly one receipt line:
`DONE: scoped PR feedback; result: <absolute artifact path>`
