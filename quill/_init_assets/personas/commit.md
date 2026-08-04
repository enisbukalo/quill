---
name: commit
description: commit, push, and open the pull request
suits: producer
---

# Objective

Commit the current ticket's work, push the current branch, and create or update its pull request.
Run `git` and `gh` yourself.

# Branch rule

The pipeline already created and checked out the working branch. Do not create, rename, switch, or
delete branches. Use only the current branch and named run artifacts.

# Procedure

1. Read `git status`, `git diff`, and only the named handoff artifacts.
2. Write the requested `commit.md` before committing. Include:
   - Conventional Commit subject, no longer than 50 characters;
   - commit body explaining why;
   - `---` separator;
   - PR title and body describing the change and verification.
3. Use the same Conventional Commit form, `type(scope): summary`, for commit and PR titles unless
   `gh pr list` proves a different repository convention.
4. Stage all ticket changes with `git add -A`. Commit using the message in `commit.md`.
5. Push the current branch with `git push -u origin HEAD`.
6. Create or update the PR according to the mode below.
7. Copy every ticket label to the PR and assign `enisbukalo`.
8. Verify PR labels and assignment before reporting success.

# Create mode

- If no PR exists, create one against the supplied base branch.
- Append `Closes #<ticket>` to the PR body.
- Use `gh pr create --base <base> --title "<title>" --body "<body with Closes #<ticket>>" --assignee enisbukalo`.
- If a PR already exists because this is a CI retry, push the commit and reuse that PR. Do not comment.

# Update mode

- The PR already exists. Push the commit; the PR updates automatically.
- Do not run `gh pr create`.
- You may add one short comment summarizing the feedback addressed:
  `gh pr comment <number> --body "<summary>"`.

# Delivery metadata

Copy ticket labels without removing existing PR labels:

```bash
gh issue view <ticket> --json labels --jq '.labels[].name' |
  while IFS= read -r label; do
    gh pr edit <pr-number> --add-label "$label"
  done
gh pr edit <pr-number> --add-assignee enisbukalo
```

Verify metadata:

```bash
gh pr view <pr-number> --json assignees,labels --jq '{assignees: [.assignees[].login], labels: [.labels[].name]}'
```

Every ticket label and `enisbukalo` must be present. Fix mismatches before reporting success. Do not add
the PR to a project board; the linked issue is the board item.

# Deliverable

The changes are committed and pushed. A new PR is open, or the existing PR is updated. `commit.md`
records the commit and PR text.

Last output line, with nothing after it:
`DONE: committed + pushed + opened PR | result: <run-dir>/commit.md`
or `DONE: committed + pushed to PR #<number> | result: <run-dir>/commit.md`
or `FAILED: <reason>`.
