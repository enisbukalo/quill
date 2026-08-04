# Global AGENTS.md (universal core only)

> WI-7: keep the **global** AGENTS.md small and universal so it doesn't bloat every phase or
> leak project facts into other repos. Project-specific conventions live in each repo's own
> `AGENTS.md` (see `AGENTS.workbench.md`). This is the universal skeleton — keep your existing
> universal wording where you have it; the point is *what stays global vs. what moves out*.

## Stays global (universal)

- **Caveman / output discipline** — terse, technical, no filler. (Your existing wording.)
- **Receipt discipline** — every spawned worker writes its full answer to a result file and
  returns exactly ONE receipt line. Grammar:
  `DONE: …` / `FAILED: …` / `PASS: …` / `BLOCK: …` /
  `FAILED: needs decision — <≤15-word question> | result: <path>`.
- **Skills come from the prompt** — load the skills named in the spawn prompt; don't guess.
- **Headless stuck-policy (no human at the keyboard):**
  1. Stuck → fire `claude-cli` (`claude -p`) for a headless answer. Resolves most.
  2. Still stuck → write the question + attempts to the result file and return
     `FAILED: needs decision — <question> | result: <path>`.
  3. **Never** "wait for human", "wait for a nod", or guess silently.
  - (Already applied: `/no_think` removed; Assumptions + Ask-Claude blocks rewritten
    headless-safe.)

## Moves OUT to the project AGENTS.md (do NOT keep these global)

These are Workbench-specific and must not leak into other repos:

- CMake globbing rules
- the explicit test-list convention (`tests/CMakeLists.txt`)
- config-struct patterns
- build-edit rule
- project board facts

→ see `AGENTS.workbench.md`.
