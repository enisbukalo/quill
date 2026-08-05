---
name: self-check-research-technical
description: verify every API, version, and tooling claim against versioned evidence
---

# Self-check

Your lane is the one the others trust for external truth. An invented API survives research, plan,
and implementation, and fails at the build gate.

For every API, method, signature, enum, annotation, flag, or tool behavior you asserted:

1. Confirm it exists **at the version this repository pins** — not in a newer release, not in a
   different language's binding, not from memory. Cite the versioned source you checked.
2. Confirm the signature: argument order, argument types, and return type. A method that exists
   with a different signature is still a build failure.
3. For anything the repository can answer directly, prefer the repository: run the tool's help,
   read the pinned config, or read the installed source rather than reasoning about it.

Remove or explicitly flag anything you could not verify. An honest unknown costs one planning
decision; an invented API costs a full implementation round.

Also confirm every executable validation seam you named — a test entry point, a lint rule, a build
flag — is real and invoked the way you described.

Correct the artifact directly. Do not design the solution.
