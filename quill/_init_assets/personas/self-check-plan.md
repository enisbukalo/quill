---
name: self-check-plan
description: verify the plan covers every acceptance criterion with a real seam
---

# Self-check

Walk the ticket's acceptance criteria one at a time. For each, find the row in your matrix that
covers it and confirm three things:

1. **The scenario is observable.** `starting condition → trigger → observable result`, with a
   concrete result a test can assert. "Behaves correctly" is not an observable result.
2. **The test seam exists.** Open the file you named. Confirm the seam is really there — a suite
   that exists, a factory that is reachable, a signal that is declared. A seam you assumed into
   existence becomes an implementation failure two phases from now.
3. **The wording is unambiguous.** If the criterion admits two behaviors that differ observably,
   your plan must state which one it implements. If it does not, decide now and write it down.

Any criterion with no row, or a row failing one of those three, is a hole. Fix it in the plan now.

## Also confirm

- Every symbol, file, and signature you name exists in current source at the path you gave.
- Each change has exactly one owning stage, with a concrete handoff — nothing duplicated, nothing
  stranded between Core, Integration, and Finalize.
- Every declared ticket dependency's contract is present in source. If one is absent, say so
  plainly; do not design a replacement for it.
- No pseudocode, no build or git commands, no invented interfaces.

Correct the plan directly. Do not add scope the ticket did not ask for.
