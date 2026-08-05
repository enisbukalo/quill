---
name: self-check-research-requirements
description: verify requirement coverage and dependency resolution against source
---

# Self-check

Your lane decides what the ticket requires and what the repository already does. Both halves are
easy to get wrong from memory.

1. **Coverage.** Re-read the ticket's scope, exclusions, and acceptance outcomes. Every one must
   appear in your matrix. A requirement you summarized away is a requirement the plan will not see.
2. **Evidence.** Every `file:line` you cited must exist and say what you claim. Open the ones you
   wrote from recall rather than from reading. Replace anything you cannot confirm with an explicit
   unknown.
3. **Dependencies.** For each dependency the ticket declares, confirm the issue is closed and its
   contract is present in tracked source, naming the symbol. If a dependency's contract is absent,
   record the absence — do not substitute your own contract for it and do not narrow the ticket's
   scope to avoid it.
4. **Inference vs evidence.** Anything you concluded rather than observed must be labeled as such.

Correct the artifact directly. Do not design the solution, propose file changes, or write
pseudocode — that is the plan's job.
