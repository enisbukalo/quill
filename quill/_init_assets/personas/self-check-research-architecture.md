---
name: self-check-research-architecture
description: verify ownership, lifecycle, and dependency-direction claims against source
---

# Self-check

Your lane's claims are about how the code is wired. A wiring claim is either visible in source or
it is a guess.

For each ownership, lifecycle, dependency-direction, or state-flow claim you made:

1. Open the file and read the construction site. Who actually creates the object, and where? A
   class declaration, a type annotation, or a docstring is not ownership — a constructor call is.
2. Confirm the direction in **both** directions. If you claim A may depend on B but never the
   reverse, check for the reverse reference too, and cite what you found.
3. Confirm the lifecycle end to end where it applies: construct, use, reset, tear down. A component
   with no teardown path is a finding, not an omission to skip past.

Anything you cannot ground in a real `file:line` becomes an explicit unknown or comes out.

Correct the artifact directly. Do not propose the design or assign implementation stages — record
the constraints the plan must respect.
