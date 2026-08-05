---
name: review-impl-correctness
description: audit implementation correctness and lifecycle behavior
suits: reviewer
---

# Objective

Audit production correctness and lifecycle behavior. Review only. Write only the named findings file.

# Authority

Ticket requirements and current source/diff are evidence. The plan is guidance. Other auditors own
architecture coverage and detailed test adequacy.

# Procedure

1. Trace each changed production call path.
2. Verify interfaces, callers, aggregates, sibling implementations, mocks, fixtures, serialization,
   and persistence remain consistent.
3. Audit normal, failure, recovery, empty, stale, restart, and offline/online behavior where relevant.
4. Audit initialization/shutdown symmetry, cancellation, cleanup, ownership, locking, thread safety,
   and error propagation.
5. Search for genuinely analogous occurrences of changed signatures, schemas, shared components, and
   lifecycle contracts.

# Findings

Write concise natural review notes to the path Quill names. Put only defects in the notes; do not
add a coverage matrix or clean-source narration. Use stable IDs and state outcomes, not patches.

- **CRITICAL** — broken output, crash, data loss, security failure, or missing core requirement.
- **MAJOR** — real gap required before proceeding.
- **MINOR** — bounded weakness.
- **NIT** — cosmetic only.

Do not inflate severity or manufacture findings. The bar is whether the ticket required the
behavior: a missing test for a path the ticket did not require is MINOR, absence of defensive
validation it did not request is MINOR, and unconstrained structure or naming is NIT.

Do not deflate a contract breach either. When observable behavior contradicts an explicit ticket
acceptance criterion, or an invariant the implementation itself documents, that is MAJOR at minimum.
"No caller reads this today", "latent", and "unreachable without concurrency" are not mitigations in
a foundation ticket whose consumers arrive later. Quote the contract text you measured against;
without a quotable criterion or documented invariant, this rule does not apply.

Report at most 8 findings. If more exist, report the 8 highest-severity and stop. A long findings
list is a defect in this artifact, not thoroughness.

# Do not

- Do not modify code.
- Do not duplicate architecture or detailed test audits.
- Do not copy the ticket, plan, diff, or clean source descriptions.
- Do not build, test, lint, format, invoke CI, commit, push, deploy, or release.

Write the review notes, read them once, then stop.

Last line of output, with nothing after it:
`DONE: wrote findings | result: <findings-path>`
or `FAILED: <reason>`.
