---
name: self-check-pr-update
description: verify every addressed review comment is really addressed, and nothing else changed
---

# Self-check

A PR update is judged on two things: it answers what reviewers actually asked, and it changes
nothing they did not ask about.

1. **Every comment accounted for.** Walk the review feedback item by item. Each one is either
   addressed in the diff, or explicitly recorded as declined with a reason. A comment you neither
   changed nor answered will come straight back.
2. **Addressed means addressed in source.** For each item you marked done, open the file and read
   the change. Confirm it resolves the comment rather than restating it, renaming around it, or
   suppressing the symptom.
3. **No unrequested scope.** Re-read your own diff. Anything not traceable to a review comment or
   the original ticket comes out — an update that quietly grows is an update that gets re-reviewed
   from scratch.
4. **The base is still green.** Confirm you did not weaken or delete a test to make the update
   pass.

Correct the artifact and the working tree directly.
