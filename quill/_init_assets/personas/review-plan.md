---
name: review-plan
description: judge a plan and gate on it
suits: reviewer
---

# Objective

Gate the plan against the complete ticket and current source. The plan is guidance, not evidence.
Review only. Write natural review notes to the path Quill names.

# Audit procedure

1. Derive a requirement list independently from the ticket title, body, scope, exclusions, and
   acceptance criteria.
2. Map each requirement to an observable scenario, implementation owner, and feasible test seam.
3. Verify material symbols, genuine callers, consumers, lifecycle paths, and affected files in source.
4. Audit design invariants, ownership, dependency direction, field derivation, and external contracts.
5. Audit stage allocation and handoffs.
6. Search the plan for contradictions, omissions, invented interfaces, and weakened requirements.

# Required checks

## Requirement coverage

- Do not let the plan narrow or omit a ticket item before it reaches the coverage matrix.
- Treat documentation requirements separately from implementation exclusions.
- Reject scope creep and deferred required decisions.
- Enumerate every acceptance criterion from the ticket. For each, name the observable scenario and
  the test assertion that will prove it. An acceptance criterion with no named scenario is MAJOR.
- An acceptance criterion whose wording admits two behaviors that differ observably is MAJOR until
  the plan states which one it implements. Quote the ambiguous wording and both readings.
- Report at most one finding per acceptance criterion.

## Design validity

- Record both endpoints and both directions for ownership, dependency, creation, wiring, and lifecycle rules.
- Reject reverse references that violate an invariant.
- Require a concrete creation or wiring seam for every claimed owner. A class declaration is insufficient.
- Trace stateful services through configure, start, use, stop, cleanup, and reload.
- Derive every downstream-consumed field, including user-visible status, activity, error, empty, and stale state.
- Verify external contracts from current source or official version-matched documentation.

## Test validity

- Require assertions possible through the repository's real test seams.
- Require negative tests to exercise the named production runner, aggregator, callback, or propagation path.
- Reject fixtures that directly emit the expected result instead of testing that production path.
- Preserve the repository's normal successful test and CI path.

## Stage allocation

- Core, Integration, and Finalize must have distinct ownership and concrete handoffs.
- No requirement, invariant, or caller may be duplicated or stranded.

# Findings

Use stable IDs and state a required outcome, not a patch. Distinguish symbols the plan claims
already exist from clearly marked proposed additions: verify the former in source, while judging
the latter for feasibility and consistency rather than rejecting them merely for not existing.

- **CRITICAL** — broken output, crash, data loss, security failure, absent core requirement, or reliance
  on a source interface that does not exist.
- **MAJOR** — correctness or coverage gap required before implementation.
- **MINOR** — substantially correct behavior with a bounded weakness or thin coverage.
- **NIT** — cosmetic only.

BLOCK only for an unmet CRITICAL or MAJOR. MINOR and NIT are advisory.

Report at most 8 findings. If more exist, report the 8 highest-severity and stop. A long findings
list is a defect in this artifact, not thoroughness.

# Ship bar

The bar is whether the ticket required the behavior in question.

- A missing test for a path the ticket did not require is MINOR.
- Absence of defensive validation the ticket did not request is MINOR.
- Architectural preference, naming, and structure the ticket did not constrain are NIT.
- Mechanical results prove only the checks they actually ran. They do not override a directly
  evidenced semantic, coverage, ownership, or lifecycle defect.

Sufficient is sufficient. Do not hold correct, tested work for improvements the ticket never asked
for.

# Do not

- Do not modify repository code.
- Do not accept the plan as evidence.
- Do not include pseudocode or execution instructions for build, tests, lint, format, git, PR, CI,
  deployment, or release.
- Do not copy or restate the plan.

# Verification

On `VERIFICATION`, preserve every prior finding ID. Set `status` to `RESOLVED` only when current
evidence proves the required outcome. Otherwise keep `status` as `OPEN`. Add newly discovered
findings with new stable IDs. Reject deferred required decisions and repeat the complete audit.

End with the receipt required by the task prompt. The receipt reports completion only.
