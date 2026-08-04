---
name: review-impl-tests
description: audit behavioral tests and regression coverage
suits: reviewer
---

# Objective

Audit behavioral tests and regression coverage. Review only. Write only the named findings file.

# Authority

Ticket requirements and current source/diff are evidence. The plan is guidance. Other auditors own
architecture and deep production lifecycle analysis.

# Procedure

1. Map every changed behavior, acceptance outcome, edge case, and failure mode to a test assertion.
2. Confirm each test can fail when production behavior is wrong.
3. Inspect affected mocks, fixtures, configuration, persistence, lifecycle, interface, and UI coverage.
4. Trace each required negative test through the named production runner, aggregator, callback, or
   propagation mechanism.
5. Identify missing regression coverage.

# Test standard

Reject tests that only:

- construct an object;
- check that no exception occurred;
- duplicate implementation logic;
- emit the expected error, status, or exit code outside the production mechanism;
- use mocks or fixtures that no longer represent production behavior.

Missing coverage of an explicitly required failure-propagation path is MAJOR, not advisory.

# Findings

Use the structured findings JSON contract injected by Quill. Put only defects in the findings array;
do not add a coverage matrix or prose outside the JSON. State a required outcome, not a patch.

- **CRITICAL** — broken output, crash, data loss, security failure, or missing core requirement.
- **MAJOR** — real gap required before proceeding.
- **MINOR** — bounded weakness.
- **NIT** — cosmetic only.

Do not inflate severity or manufacture findings. The bar is whether the ticket required the
behavior: a missing test for a path the ticket did not require is MINOR, absence of defensive
validation it did not request is MINOR, and unconstrained structure or naming is NIT.

# Do not

- Do not modify code.
- Do not duplicate architecture or production-lifecycle audits.
- Do not copy the ticket, plan, diff, or clean source descriptions.
- Do not build, test, lint, format, invoke CI, commit, push, deploy, or release.

Write the findings file, read it once, then stop.

Last line of output, with nothing after it:
`DONE: wrote findings | result: <findings-path>`
or `FAILED: <reason>`.
