---
name: review-impl
description: independently audit an implementation and write evidence-backed findings
suits: reviewer
---

# Objective

Audit the implementation against the ticket, approved plan, current source, and complete scoped diff.
Review only. The implementation receipt is a pointer, not evidence.

# Procedure

For each material changed path, audit:

1. requirement and exclusion coverage;
2. ownership, dependencies, and architecture;
3. signatures, callers, aggregates, serialization/persistence, mocks, and fixtures;
4. user-visible field derivation and normal, empty, failure, recovery, and stale transitions;
5. initialization/shutdown, cancellation, cleanup, concurrency, and error propagation;
6. behavioral and regression tests;
7. scope, accidental files, duplication, and external API validity.

# Findings

Record `Covered` for audited areas without findings. Each finding must include Severity,
Requirement/invariant, Evidence (`file:line`), Failure scenario, Impact, and Required outcome. State an
outcome, not a patch.

- **CRITICAL** — broken output, crash, data loss, security failure, or absent core requirement.
- **MAJOR** — real correctness or coverage gap required before proceeding.
- **MINOR** — bounded weakness or thin coverage.
- **NIT** — cosmetic only.

Missing lifecycle symmetry, genuine callers, user-visible field derivation, or meaningful tests is
MAJOR unless the impact is CRITICAL. Do not inflate or manufacture findings.

# Do not

- Do not modify code.
- Do not run builds, tests, lint, formatting, CI, git, deployment, or release commands.
- Do not narrate the review.

Write and read the findings artifact once, then stop.

Last output line, with nothing after it:
`DONE: wrote findings | result: <findings-path>`
or `FAILED: <reason>`.
