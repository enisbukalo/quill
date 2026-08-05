---
name: self-check-pr-update
description: verify every addressed review comment is really addressed, and nothing else changed
---

# Self-check

Re-read every active feedback item, the original ticket, and the artifact produced by this phase.

If the original phase is update scope, correct only the scope artifact: ensure every feedback item
has an evidence-backed required outcome, preserve unresolved ambiguity, and remove unrelated scope.
Do not edit source. If the original phase is update implementation, inspect the actual diff and
tests, then correct source, tests, and the implementation artifact only within the original phase's
authority. Verify that each claimed result addresses the feedback's root issue and that no test was
deleted, weakened, or bypassed.

Do not invent a "declined" outcome. Unresolved or conflicting feedback requires an explicit decision
or remains incomplete; it is not silently waived by the agent.
