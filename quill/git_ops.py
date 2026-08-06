"""GitHub metadata access for the driver.

quill is data-driven: **agent phases own most git/gh side effects** (a branch phase runs
``git checkout -b`` in the repo's own convention; a commit phase runs ``git commit``/``git push``/
``gh pr create``). The driver never stages, commits, branches, pushes, or opens PRs — that logic
lives in personas, not Python, so it is fully configurable per repo.

What the driver reads is:

* the **ticket body** — the goal, injected into every phase's prompt; and
* in ``--update`` mode, the **open PR for that ticket** plus **all of its review feedback**, so an
  update run resumes on the existing branch and every phase sees what reviewers asked for.

The narrow mutations are delivery invariants: Quill repairs a missing canonical ``Closes #N``
reference after PR creation, and a validated PR-review PASS merges the exact reviewed head before
deleting only its remote feature branch.

It shells out through an injectable :class:`Runner` so tests can assert the command without a live
repo.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol


PR_LINK_VERIFY_ATTEMPTS = 5
PR_LINK_VERIFY_INTERVAL_SECONDS = 1.0
_sleep = time.sleep


class Runner(Protocol):
    """Runs a command and returns stdout. Raises on non-zero exit."""

    def __call__(self, args: Sequence[str]) -> str: ...


class GitError(RuntimeError):
    """A git/gh command failed."""


@dataclass(slots=True)
class SubprocessRunner:
    """Default runner: shell out in ``directory``, raise :class:`GitError` on failure."""

    directory: str

    def __call__(self, args: Sequence[str]) -> str:
        try:
            proc = subprocess.run(
                list(args),
                cwd=self.directory,
                capture_output=True,
                text=True,
                # Force UTF-8: git/gh emit UTF-8 (issue bodies, commit text carry emoji, box-drawing,
                # accents), but on Windows text=True decodes with cp1252 by default — which has 5
                # undefined bytes (0x81/0x8d/0x8f/0x90/0x9d). A body with any of them raised
                # UnicodeDecodeError in the reader thread, so `gh issue view` returned empty and the
                # run failed with "ticket has no fetchable body". errors="replace" degrades a stray
                # byte to U+FFFD instead of crashing the fetch.
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise GitError(f"could not launch {args[0]!r}: {exc}") from exc
        if proc.returncode != 0:
            raise GitError(f"{' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout.strip()


@dataclass(frozen=True, slots=True)
class PullRequest:
    """The open PR an ``--update`` run targets."""

    number: int
    branch: str
    title: str
    url: str
    head_sha: str = ""
    committed_at: str = ""


class AmbiguousPullRequest(GitError):
    """More than one open PR is associated with a ticket."""


@dataclass(frozen=True, slots=True)
class FeedbackItem:
    id: str
    source: str
    author: str
    body: str
    actionable_at: str
    created_at: str = ""
    updated_at: str = ""
    state: str = ""
    path: str = ""
    line: int | None = None
    thread_id: str = ""
    resolved: bool | None = None
    viewer_can_resolve: bool | None = None
    url: str = ""

    def prompt_block(self) -> str:
        where = f" @ {self.path}:{self.line}" if self.path else ""
        return f"[{self.source}:{self.id}] {self.author}{where}:\n{self.body}"


@dataclass(frozen=True, slots=True)
class FeedbackSnapshot:
    pr: PullRequest
    selected: tuple[FeedbackItem, ...]
    excluded_old: int = 0
    excluded_blank: int = 0
    excluded_malformed: int = 0

    def render_prompt(self) -> str:
        return "\n\n".join(item.prompt_block() for item in self.selected)


#: A check that finished and succeeded (or never had to run). ``NEUTRAL`` and ``SKIPPED`` are
#: deliberately successes: a conditional job that opted out must not block a PR forever.
_PASSING_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
#: A check that has not finished. Anything COMPLETED with a conclusion outside
#: :data:`_PASSING_CONCLUSIONS` (FAILURE, TIMED_OUT, CANCELLED, ACTION_REQUIRED, ...) is a failure.
_PENDING_STATES = frozenset(
    {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED", "EXPECTED"}
)


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One CI check on a PR's head commit, normalised across GitHub's two shapes."""

    name: str
    #: ``"pass"`` / ``"fail"`` / ``"pending"`` — normalised so callers never re-derive it.
    state: str
    url: str
    status: str = ""
    conclusion: str = ""

    @property
    def run_id(self) -> str | None:
        """The Actions run id in this check's URL, for ``gh run view --log-failed``."""
        match = re.search(r"/actions/runs/(\d+)", self.url)
        return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class ChecksStatus:
    """The aggregate CI verdict for a PR, plus the checks behind it."""

    checks: tuple[CheckRun, ...] = ()

    @property
    def reported(self) -> bool:
        """False when GitHub lists no checks at all.

        Distinct from "passing". For a while after a push, a PR whose workflows are about to run
        reports nothing at all; treating that as success would let a run sail through a CI gate
        that had not started yet, so the caller waits it out instead.
        """
        return bool(self.checks)

    @property
    def pending(self) -> tuple[CheckRun, ...]:
        return tuple(c for c in self.checks if c.state == "pending")

    @property
    def failed(self) -> tuple[CheckRun, ...]:
        return tuple(c for c in self.checks if c.state == "fail")

    @property
    def settled(self) -> bool:
        """True once every reported check has finished, whatever its verdict."""
        return self.reported and not self.pending


@dataclass(slots=True)
class GitOps:
    """The driver's GitHub metadata surface and narrow PR-delivery invariants.

    Agent phases still own branches, commits, pushes, PR creation, and project-board changes.
    Quill repairs missing closing-ticket references and completes validated PR-review passes.
    """

    run: Runner

    def issue_body(self, ticket: int) -> str:
        """`gh issue view <n>` title+body JSON — the goal, injected into every phase's prompt."""
        return self.run(["gh", "issue", "view", str(ticket), "--json", "title,body"])

    def pr_for_ticket(self, ticket: int) -> PullRequest | None:
        """The open PR for ``ticket``, or ``None`` if there isn't one.

        Searches open PRs for the ticket reference rather than assuming a branch-naming convention:
        the branch phase names branches to whatever convention the repo already uses, so
        ``ticket-33-*`` is a guess we cannot rely on. A PR counts as the ticket's when its body,
        title, or head branch mentions the number (``#33``, ``ticket-33``, ...). Ambiguous matches
        are rejected rather than guessed.
        """
        raw = self.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--search",
                str(ticket),
                "--limit",
                "20",
                "--json",
                "number,headRefName,title,body,url,headRefOid",
            ]
        )
        matches = [entry for entry in _json_list(raw) if _mentions_ticket(entry, ticket)]
        if len(matches) > 1:
            numbers = ", ".join(f"#{entry.get('number')}" for entry in matches)
            raise AmbiguousPullRequest(
                f"ticket #{ticket} matches multiple open PRs ({numbers}); close or rename the "
                "unrelated PR before starting an update"
            )
        if not matches:
            return None
        pr = _as_pull_request(matches[0])
        return pr

    def pr_target_for_ticket(self, ticket: int) -> PullRequest | None:
        """Resolve one open PR and require its GitHub commit boundary metadata."""
        pr = self.pr_for_ticket(ticket)
        if pr is None:
            return None
        details = self.run(["gh", "pr", "view", str(pr.number), "--json", "headRefOid,commits"])
        return _with_boundary(pr, details)

    def pr_for_branch(self, branch: str) -> PullRequest | None:
        """The open PR whose head is exactly ``branch``, without full-text search indexing."""
        raw = self.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--head",
                branch,
                "--limit",
                "1",
                "--json",
                "number,headRefName,title,body,url,headRefOid",
            ]
        )
        for entry in _json_list(raw):
            pr = _as_pull_request(entry)
            if pr is not None and pr.branch == branch:
                return pr
        return None

    def ensure_pr_closes_ticket(self, pr_number: int, ticket: int) -> str:
        """Ensure ``pr_number`` closes ``ticket`` and verify GitHub recognized the reference.

        GitHub's parsed ``closingIssuesReferences`` is authoritative. A title containing ``#N``
        is not a closing reference, and searching the body text can be fooled by code blocks or
        quotations. When the parsed reference is absent, append one canonical line without
        replacing the agent-authored body, then read the parsed metadata again.
        """
        body, closing = _closing_issue_metadata(
            self.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "body,closingIssuesReferences",
                ]
            ),
            pr_number,
        )
        if ticket in closing:
            return "closing ticket already linked"

        separator = "\n\n" if body.rstrip() else ""
        updated = f"{body.rstrip()}{separator}Closes #{ticket}"
        self.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/pulls/{pr_number}",
                "--method",
                "PATCH",
                "--raw-field",
                f"body={updated}",
            ]
        )
        for attempt in range(PR_LINK_VERIFY_ATTEMPTS):
            _, verified = _closing_issue_metadata(
                self.run(
                    [
                        "gh",
                        "pr",
                        "view",
                        str(pr_number),
                        "--json",
                        "body,closingIssuesReferences",
                    ]
                ),
                pr_number,
            )
            if ticket in verified:
                return "added missing closing-ticket reference"
            if attempt + 1 < PR_LINK_VERIFY_ATTEMPTS:
                _sleep(PR_LINK_VERIFY_INTERVAL_SECONDS)
        raise GitError(f"PR #{pr_number} still does not close ticket #{ticket} after body repair")

    def pr_feedback(self, pr_number: int) -> str:
        """Compatibility helper returning every feedback item as prompt text."""
        blocks = [
            *_top_level_comments(
                self.run(["gh", "pr", "view", str(pr_number), "--json", "comments"])
            ),
            *_review_summaries(self.run(["gh", "pr", "view", str(pr_number), "--json", "reviews"])),
            *_inline_comments(
                self.run(
                    [
                        "gh",
                        "api",
                        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
                        "--paginate",
                    ]
                )
            ),
        ]
        return "\n\n".join(blocks)

    def feedback_snapshot(self, pr: PullRequest, *, include_all: bool = False) -> FeedbackSnapshot:
        """Capture typed PR feedback strictly newer than the PR-head commit boundary.

        Three sources, because no single ``gh`` call returns them all:

        * ``gh pr view --json comments`` — conversation-tab (top-level) comments.
        * ``gh pr view --json reviews`` — each review's summary body (the approve /
          request-changes note), which ``comments`` omits.
        * ``gh api .../pulls/<n>/comments`` — inline review-thread comments anchored to a file and
          line. These are the most actionable and are absent from BOTH ``--json`` shapes above,
          so the extra call is not redundant.

        Empty-bodied entries (a bare approval, a review whose content is all inline) are dropped —
        they would otherwise pad the prompt with blank bullets.
        """
        conversation = self.run(["gh", "pr", "view", str(pr.number), "--json", "comments,reviews"])
        inline = self.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/pulls/{pr.number}/comments",
                "--paginate",
            ]
        )
        items, blank = _feedback_items(conversation, inline)
        try:
            thread_metadata = self._review_thread_metadata(pr.number)
        except GitError:
            thread_metadata = {}
        enriched: list[FeedbackItem] = []
        for item in items:
            if item.source == "inline":
                thread_id, resolved, can_resolve = thread_metadata.get(
                    item.id, (item.thread_id, item.resolved, item.viewer_can_resolve)
                )
                item = replace(
                    item,
                    thread_id=thread_id,
                    resolved=resolved,
                    viewer_can_resolve=can_resolve,
                )
            enriched.append(item)
        items = enriched
        boundary = _parse_github_time(pr.committed_at)
        selected: list[FeedbackItem] = []
        old = malformed = 0
        for item in items:
            timestamp = _parse_github_time(item.actionable_at)
            if timestamp is None or (boundary is None and not include_all):
                malformed += 1
            elif include_all or (boundary is not None and timestamp > boundary):
                selected.append(item)
            else:
                old += 1
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        selected.sort(
            key=lambda item: (
                _parse_github_time(item.actionable_at) or epoch,
                item.source,
                item.id,
            )
        )
        return FeedbackSnapshot(pr, tuple(selected), old, blank, malformed)

    def _review_thread_metadata(self, pr_number: int) -> dict[str, tuple[str, bool, bool]]:
        """Map inline comment node IDs to their review-thread resolution metadata."""
        raw_repo = self.run(["gh", "repo", "view", "--json", "nameWithOwner"])
        try:
            repo_data = json.loads(raw_repo)
            owner, name = str(repo_data["nameWithOwner"]).split("/", 1)
        except (ValueError, KeyError) as exc:
            raise GitError("could not resolve repository identity for review threads") from exc
        # `comments(first:N)` is nested inside a 100-thread page, so each unit costs 100 nodes.
        # Asking for 100 made one page ~10,100 nodes; GitHub scores requested nodes, not returned
        # ones. Only the comment IDs are used, to map a comment back to its thread — and a review
        # thread with more than 20 comments is vanishingly rare.
        query = """query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{id isResolved viewerCanResolve comments(first:20){nodes{id}}}}}}}"""
        after = ""
        metadata: dict[str, tuple[str, bool, bool]] = {}
        while True:
            args = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={pr_number}",
            ]
            if after:
                args.extend(["-F", f"after={after}"])
            raw = self.run(args)
            try:
                data = json.loads(raw)
                threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes = threads.get("nodes") or []
            except (ValueError, KeyError, TypeError) as exc:
                raise GitError(f"PR #{pr_number} returned malformed review threads") from exc
            for thread in nodes:
                if not isinstance(thread, dict):
                    continue
                thread_id = str(thread.get("id") or "")
                value = (
                    thread_id,
                    thread.get("isResolved") is True,
                    thread.get("viewerCanResolve") is True,
                )
                comments = thread.get("comments")
                for comment in comments.get("nodes", []) if isinstance(comments, dict) else []:
                    if isinstance(comment, dict) and comment.get("id"):
                        metadata[str(comment["id"])] = value
            page = threads.get("pageInfo") or {}
            if not isinstance(page, dict) or page.get("hasNextPage") is not True:
                break
            after = str(page.get("endCursor") or "")
            if not after:
                raise GitError(f"PR #{pr_number} review-thread pagination returned no cursor")
        return metadata

    def resolve_review_thread(self, thread_id: str) -> None:
        query = (
            "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}"
        )
        self.run(["gh", "api", "graphql", "-f", f"query={query}", "-F", f"id={thread_id}"])

    def reply_review_thread(self, thread_id: str, body: str) -> None:
        query = "mutation($id:ID!,$body:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id,body:$body}){comment{id}}}"
        self.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"id={thread_id}",
                "-f",
                f"body={body}",
            ]
        )

    def pr_checks(self, pr_number: int) -> ChecksStatus:
        """Every CI check on ``pr_number``'s head commit.

        Reads ``gh pr view --json statusCheckRollup`` rather than ``gh pr checks``: the latter
        signals its verdict through the **exit code** (non-zero when checks fail, when they are
        still pending, and when the branch has no checks at all), which :class:`SubprocessRunner`
        turns into a :class:`GitError` that discards the very output we need. The rollup exits 0
        and carries the same data.
        """
        raw = self.run(["gh", "pr", "view", str(pr_number), "--json", "statusCheckRollup"])
        return ChecksStatus(checks=tuple(_parse_checks(raw)))

    def pr_head_sha(self, pr_number: int) -> str:
        """Return the PR's current remote head OID."""
        raw = self.run(["gh", "pr", "view", str(pr_number), "--json", "headRefOid"])
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise GitError(f"PR #{pr_number} returned malformed head metadata") from exc
        sha = data.get("headRefOid") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not sha:
            raise GitError(f"PR #{pr_number} returned no head SHA")
        return sha

    def merge_reviewed_pr(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        expected_branch: str,
        expected_base: str,
        pr_checks_required: bool = True,
    ) -> str:
        """Merge one validated PR head, then delete only its remote feature branch.

        Every mutable boundary is re-read immediately before the merge. The reviewed commit,
        branch, base, configured CI policy, and GitHub merge state must still match; otherwise no
        merge is attempted. A repository may permit an empty check rollup, but any reported pending
        or failed check still blocks. ``--match-head-commit`` closes the final race between
        validation and mutation. The local branch is intentionally never checked out or deleted.
        """
        fields = (
            "number,state,isDraft,mergeable,mergeStateStatus,headRefName,headRefOid,"
            "baseRefName,statusCheckRollup"
        )
        raw = self.run(["gh", "pr", "view", str(pr_number), "--json", fields])
        try:
            metadata = json.loads(raw)
        except ValueError as exc:
            raise GitError(f"PR #{pr_number} returned malformed merge metadata") from exc
        if not isinstance(metadata, dict):
            raise GitError(f"PR #{pr_number} returned malformed merge metadata")

        state = str(metadata.get("state") or "").upper()
        head_sha = str(metadata.get("headRefOid") or "")
        head_branch = str(metadata.get("headRefName") or "")
        base_branch = str(metadata.get("baseRefName") or "")
        if state != "OPEN":
            raise GitError(f"PR #{pr_number} is {state or 'not open'}")
        if metadata.get("isDraft") is True:
            raise GitError(f"PR #{pr_number} is still a draft")
        if head_sha != expected_head_sha:
            raise GitError(
                f"PR #{pr_number} moved from {expected_head_sha[:12]} to {head_sha[:12]}"
            )
        if head_branch != expected_branch:
            raise GitError(
                f"PR #{pr_number} head branch changed from {expected_branch!r} to {head_branch!r}"
            )
        if base_branch != expected_base:
            raise GitError(
                f"PR #{pr_number} base branch changed from {expected_base!r} to {base_branch!r}"
            )
        if not head_branch or head_branch == base_branch:
            raise GitError(f"PR #{pr_number} has an unsafe remote branch target")
        if str(metadata.get("mergeable") or "").upper() != "MERGEABLE":
            raise GitError(f"PR #{pr_number} is not mergeable")
        merge_state = str(metadata.get("mergeStateStatus") or "").upper()
        if merge_state != "CLEAN":
            raise GitError(f"PR #{pr_number} merge state is {merge_state or 'unknown'}, not CLEAN")

        raw_checks = metadata.get("statusCheckRollup")
        if not isinstance(raw_checks, list) or any(
            not isinstance(check, dict) for check in raw_checks
        ):
            raise GitError(f"PR #{pr_number} returned a malformed CI check rollup")
        checks = ChecksStatus(checks=tuple(_parse_checks(raw)))
        if not checks.reported and pr_checks_required:
            raise GitError(f"PR #{pr_number} has no reported CI checks")
        if checks.pending:
            names = ", ".join(check.name for check in checks.pending)
            raise GitError(f"PR #{pr_number} still has pending CI checks: {names}")
        if checks.failed:
            names = ", ".join(check.name for check in checks.failed)
            raise GitError(f"PR #{pr_number} has failed CI checks: {names}")

        self.run(
            [
                "gh",
                "pr",
                "merge",
                str(pr_number),
                "--merge",
                "--match-head-commit",
                expected_head_sha,
            ]
        )
        verified_raw = self.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "state,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName",
            ]
        )
        try:
            verified = json.loads(verified_raw)
        except ValueError as exc:
            raise GitError(f"PR #{pr_number} returned malformed post-merge metadata") from exc
        if not isinstance(verified, dict) or str(verified.get("state") or "").upper() != "MERGED":
            raise GitError(f"PR #{pr_number} did not report a completed merge")
        if (
            verified.get("headRefOid") != expected_head_sha
            or verified.get("headRefName") != expected_branch
            or verified.get("baseRefName") != expected_base
        ):
            raise GitError(f"PR #{pr_number} post-merge metadata does not match the reviewed head")
        merge_commit = verified.get("mergeCommit")
        if not isinstance(merge_commit, dict) or not merge_commit.get("oid"):
            raise GitError(f"PR #{pr_number} returned no merge commit")

        remote_ref = f"refs/heads/{expected_branch}"
        if self.run(["git", "ls-remote", "--heads", "origin", remote_ref]).strip():
            self.run(["git", "push", "origin", f":{remote_ref}"])
            if self.run(["git", "ls-remote", "--heads", "origin", remote_ref]).strip():
                raise GitError(
                    f"PR #{pr_number} merged, but remote branch {expected_branch!r} still exists"
                )
            return f"merged PR #{pr_number} and deleted remote branch {expected_branch}"
        return f"merged PR #{pr_number}; remote branch {expected_branch} was already deleted"

    def local_head_sha(self) -> str:
        return self.run(["git", "rev-parse", "HEAD"]).strip()

    def pr_comments(self, pr_number: int) -> list[dict[str, Any]]:
        raw = self.run(["gh", "pr", "view", str(pr_number), "--json", "comments"])
        return _field_list(raw, "comments")

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        self.run(["gh", "pr", "comment", str(pr_number), "--body", body])

    def upsert_pr_comment(self, pr_number: int, marker: str, body: str) -> str:
        """Create or update the unique REST issue comment containing ``marker``.

        ``gh pr view --json comments`` exposes GraphQL node IDs but does not reliably expose the
        numeric database ID required by the REST PATCH endpoint. Read the issue-comment endpoint
        directly so creating and updating use GitHub's real response contract.
        """
        raw = self.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            ]
        )
        matches = [
            comment
            for comment in _paginated_json_list(raw)
            if marker in str(comment.get("body") or "")
        ]
        if len(matches) > 1:
            raise GitError(f"PR #{pr_number} has multiple managed comments for {marker}")
        if not matches:
            self.post_pr_comment(pr_number, body)
            return "created"
        comment_id = matches[0].get("id")
        if type(comment_id) is not int:
            raise GitError(f"PR #{pr_number} managed comment has no numeric database ID")
        self.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            ]
        )
        return "updated"

    def workspace_status(self) -> str:
        """Return tracked and untracked workspace changes, excluding ignored build outputs."""
        return self.run(["git", "status", "--porcelain", "--untracked-files=all"])

    def failed_check_log(self, run_id: str) -> str:
        """The failing log for an Actions run — extracted and size-bounded — or ``""``.

        Best-effort by design: this is context that makes a revise better, not the gate decision
        itself, which has already been made from the check states. A log that will not download
        must not turn a clean BLOCK into a crash.

        ``gh run view --log-failed`` returns only the steps GitHub marked failed. That is precise
        when a workflow fails loudly, but useless when the workflow hides the real failure behind a
        summary/gate step — e.g. a step that asserts ``$TEST_OUTCOME == success`` and exits 1. Then
        the only "failed" step is the gate, whose log says which *outcome variable* was false but
        nothing about which test broke, and the revise agent is left fixing a failure it cannot see.
        So when the failed-step log carries no actionable cause, fall back to the whole run log and
        extract the region around the failure markers — the actual error output. Bounded so a large
        matrix build cannot overflow the agent's context.
        """
        failed = self._safe_log(["gh", "run", "view", run_id, "--log-failed"])
        if _has_actionable_detail(failed):
            return _clip_log(failed)
        full = self._safe_log(["gh", "run", "view", run_id, "--log"])
        return _extract_failures(full) or _clip_log(failed) or _clip_log(full)

    def _safe_log(self, args: Sequence[str]) -> str:
        """Run a log-fetch command, degrading a download failure to ``""`` instead of a crash."""
        try:
            return self.run(args)
        except GitError:
            return ""


# -- CI failure-log extraction ----------------------------------------------------

#: Ceiling on failure-log text handed to a revise agent, in characters. Large enough for a real
#: test failure with surrounding context, small enough that a multi-job matrix log cannot swamp the
#: prompt (and cost).
_FAILURE_LOG_BUDGET = 16000

#: Lines that mark where a build or test actually went wrong, across common toolchains — compilers,
#: ctest/gtest, pytest, go test, cargo, node. Deliberately vocabulary-based, not tied to one
#: framework's format, so the extraction stays generic across whatever a target repo runs.
_FAILURE_MARKERS = re.compile(
    r"(?i)("
    r"\bfailed\b|\bfailure\b|\bfails?\b|"
    r"\berror\b|error:|error c\d|"
    r"assert|assertion|expected|"
    r"panic:|traceback|exception|"
    r"segmentation fault|core dumped|undefined reference|"
    r"\bLNK\d|\bnot ok\b|✗|✘|✕"
    r")"
)

#: The CI wrapper's own generic "a step exited nonzero" noise. On its own it names no cause, so a
#: failed-step log containing only this (plus outcome echoes) is a stub, not actionable detail.
_WRAPPER_NOISE = re.compile(r"(?i)(process completed with exit code|##\[error\])")

#: A summary/outcome line ("Test suite: failure", "TEST_OUTCOME: failure") — a verdict, not a cause.
#: No leading ``\b``: ``TEST_OUTCOME`` has no word boundary before "outcome" (``_`` is a word char).
_OUTCOME_SUMMARY = re.compile(r"(?i)(outcome|suite)[^\n]*(success|failure)")


def _has_actionable_detail(log: str) -> bool:
    """True when a ``--log-failed`` result names a real cause, not just gate/summary noise.

    A summary-gate failure looks like ``TEST_OUTCOME: failure`` + ``##[error]Process completed with
    exit code 1``: failure *markers*, but no cause. Require a marker on a line that is neither the CI
    wrapper's generic exit line nor an outcome summary, so those stubs fall through to the full-log
    fallback while a workflow that fails loudly keeps its precise, cheaper path.
    """
    for line in log.splitlines():
        if not _FAILURE_MARKERS.search(line):
            continue
        if _WRAPPER_NOISE.search(line) or _OUTCOME_SUMMARY.search(line):
            continue
        return True
    return False


def _extract_failures(log: str, budget: int = _FAILURE_LOG_BUDGET) -> str:
    """The failure-relevant slice of a full run log: a window around each marker, plus the tail.

    Generic across toolchains — it keys off failure vocabulary, not any one framework's format —
    and bounded to ``budget`` characters (keeping the tail, where run summaries live) so a huge
    matrix log cannot overflow the revise prompt. ``""`` for an empty log.
    """
    lines = log.splitlines()
    if not any(line.strip() for line in lines):
        return ""
    keep = [False] * len(lines)
    for index, line in enumerate(lines):
        if _FAILURE_MARKERS.search(line):
            for near in range(max(0, index - 3), min(len(lines), index + 13)):
                keep[near] = True
    picked = [line for line, wanted in zip(lines, keep) if wanted]
    if not picked:
        picked = lines[-200:]  # no marker matched — the tail beats handing back nothing
    return _clip_log("\n".join(picked), budget)


def _clip_log(log: str, budget: int = _FAILURE_LOG_BUDGET) -> str:
    """Trim ``log`` to ``budget`` characters, keeping the tail. Empty input passes through."""
    text = log.strip()
    if len(text) <= budget:
        return text
    return "…(truncated)…\n" + text[-budget:]


# -- PR parsing -------------------------------------------------------------------


def pr_target_for_repo(run: Runner, repo: str, ticket: int) -> PullRequest | None:
    """Resolve a ticket PR without requiring a local checkout of ``repo``."""
    raw = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            str(ticket),
            "--limit",
            "20",
            "--json",
            "number,headRefName,title,body,url,headRefOid",
        ]
    )
    matches = [entry for entry in _json_list(raw) if _mentions_ticket(entry, ticket)]
    if len(matches) > 1:
        numbers = ", ".join(f"#{entry.get('number')}" for entry in matches)
        raise AmbiguousPullRequest(
            f"ticket #{ticket} matches multiple open PRs ({numbers}); close or rename the "
            "unrelated PR before starting a review"
        )
    if not matches:
        return None
    pr = _as_pull_request(matches[0])
    if pr is None:
        return None
    details = run(
        [
            "gh",
            "pr",
            "view",
            str(pr.number),
            "--repo",
            repo,
            "--json",
            "headRefOid,commits",
        ]
    )
    return _with_boundary(pr, details)


def _parse_checks(raw: str) -> list[CheckRun]:
    """Normalise a ``statusCheckRollup`` payload into :class:`CheckRun`s.

    GitHub returns two different shapes in the same array — ``CheckRun`` entries (Actions and other
    apps: ``name`` + ``status`` + ``conclusion``) and ``StatusContext`` entries (the older commit
    status API: ``context`` + ``state``). Both are normalised here so no caller has to know which
    kind of CI a repo happens to use.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    rollup = data.get("statusCheckRollup") if isinstance(data, dict) else None
    if not isinstance(rollup, list):
        return []

    checks: list[CheckRun] = []
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("context") or "check")
        url = str(entry.get("detailsUrl") or entry.get("targetUrl") or "")
        status = str(entry.get("status") or "").upper()
        # A StatusContext has no `status`; its `state` carries both progress and verdict.
        conclusion = str(entry.get("conclusion") or entry.get("state") or "").upper()

        if status in _PENDING_STATES or (not status and conclusion in _PENDING_STATES):
            state = "pending"
        elif conclusion in _PASSING_CONCLUSIONS:
            state = "pass"
        elif not conclusion:
            # Reported but says nothing about itself: assume it is still starting rather than
            # failing a PR on a shape we do not recognise.
            state = "pending"
        else:
            state = "fail"
        checks.append(
            CheckRun(
                name=name,
                state=state,
                url=url,
                status=status,
                conclusion=conclusion,
            )
        )
    return checks


def _json_list(raw: str) -> list[dict[str, Any]]:
    """Parse ``raw`` as a JSON array of objects; ``[]`` on anything unexpected.

    A gh read that returns nothing usable must not crash the run — the caller degrades to "no PR"
    or "no feedback", which the CLI reports as a clear error instead of a traceback.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _closing_issue_metadata(raw: str, pr_number: int) -> tuple[str, set[int]]:
    """Parse one PR body's text and GitHub-resolved closing issue numbers."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GitError(f"PR #{pr_number} returned malformed closing-issue metadata") from exc
    if not isinstance(data, dict) or not isinstance(data.get("body"), str):
        raise GitError(f"PR #{pr_number} returned malformed closing-issue metadata")
    references = data.get("closingIssuesReferences")
    if not isinstance(references, list):
        raise GitError(f"PR #{pr_number} returned malformed closing-issue metadata")
    numbers = {
        number
        for entry in references
        if isinstance(entry, dict) and isinstance((number := entry.get("number")), int)
    }
    return data["body"], numbers


def _as_pull_request(entry: dict[str, Any]) -> PullRequest | None:
    number = entry.get("number")
    branch = entry.get("headRefName")
    if not isinstance(number, int) or not isinstance(branch, str) or not branch:
        return None
    return PullRequest(
        number=number,
        branch=branch,
        title=str(entry.get("title") or ""),
        url=str(entry.get("url") or ""),
        head_sha=str(entry.get("headRefOid") or ""),
    )


def _with_boundary(pr: PullRequest, raw: str) -> PullRequest:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GitError(f"PR #{pr.number} returned malformed head metadata") from exc
    commits = data.get("commits") if isinstance(data, dict) else None
    last = commits[-1] if isinstance(commits, list) and commits else {}
    committed_at = last.get("committedDate") if isinstance(last, dict) else None
    head_sha = data.get("headRefOid") if isinstance(data, dict) else None
    if not isinstance(head_sha, str) or not head_sha:
        head_sha = pr.head_sha
    if (
        not head_sha
        or not isinstance(committed_at, str)
        or _parse_github_time(committed_at) is None
    ):
        raise GitError(f"PR #{pr.number} has no usable head SHA/committedDate boundary")
    return PullRequest(pr.number, pr.branch, pr.title, pr.url, head_sha, committed_at)


def _parse_github_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _mentions_ticket(entry: dict[str, Any], ticket: int) -> bool:
    """True when this PR's title, body, or head branch references ``ticket``.

    ``gh pr list --search`` is a full-text match, so it also returns PRs that merely contain the
    digits somewhere (a diff line, an unrelated number). Requiring the reference to appear as
    ``#33`` or delimited by non-digits (``ticket-33-...``) keeps #33 from matching #330.
    """
    import re

    haystack = " ".join(
        str(entry.get(key) or "") for key in ("title", "body", "headRefName", "url")
    )
    return re.search(rf"(?<!\d){ticket}(?!\d)", haystack) is not None


# -- comment flattening -----------------------------------------------------------


def _author_of(entry: dict[str, Any]) -> str:
    """Author login from either gh JSON shape: ``author.login`` or REST's ``user.login``."""
    for key in ("author", "user"):
        who = entry.get(key)
        if isinstance(who, dict):
            login = who.get("login")
            if isinstance(login, str) and login:
                return login
    return "unknown"


def _field_list(raw: str, field: str) -> list[dict[str, Any]]:
    """Pull ``field`` (a list of objects) out of a ``gh pr view --json <field>`` payload."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    value = data.get(field)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _body_of(entry: dict[str, Any]) -> str:
    body = entry.get("body")
    return body.strip() if isinstance(body, str) else ""


def _feedback_items(conversation: str, inline: str) -> tuple[list[FeedbackItem], int]:
    """Preserve feedback identity and timestamps; return items and blank-body count."""
    items: list[FeedbackItem] = []
    blank = 0
    for comment in _field_list(conversation, "comments"):
        body = _body_of(comment)
        if not body:
            blank += 1
            continue
        created = str(comment.get("createdAt") or "")
        updated = str(comment.get("updatedAt") or created)
        items.append(
            FeedbackItem(
                id=str(comment.get("id") or comment.get("url") or "comment"),
                source="comment",
                author=_author_of(comment),
                body=body,
                actionable_at=updated,
                created_at=created,
                updated_at=updated,
                url=str(comment.get("url") or ""),
            )
        )
    for review in _field_list(conversation, "reviews"):
        body = _body_of(review)
        if not body:
            blank += 1
            continue
        submitted = str(review.get("submittedAt") or "")
        items.append(
            FeedbackItem(
                id=str(review.get("id") or review.get("url") or "review"),
                source="review",
                author=_author_of(review),
                body=body,
                actionable_at=submitted,
                created_at=submitted,
                updated_at=submitted,
                state=str(review.get("state") or ""),
                url=str(review.get("url") or ""),
            )
        )
    for comment in _paginated_json_list(inline):
        body = _body_of(comment)
        if not body:
            blank += 1
            continue
        created = str(comment.get("created_at") or "")
        updated = str(comment.get("updated_at") or created)
        line = comment.get("line") or comment.get("original_line")
        items.append(
            FeedbackItem(
                id=str(comment.get("node_id") or comment.get("id") or "inline"),
                source="inline",
                author=_author_of(comment),
                body=body,
                actionable_at=updated,
                created_at=created,
                updated_at=updated,
                path=str(comment.get("path") or ""),
                line=line if isinstance(line, int) else None,
                thread_id=str(comment.get("pull_request_review_id") or ""),
                url=str(comment.get("html_url") or ""),
            )
        )
    return items, blank


def _top_level_comments(raw: str) -> list[str]:
    return [
        f"[comment] {_author_of(c)}:\n{body}"
        for c in _field_list(raw, "comments")
        if (body := _body_of(c))
    ]


def _review_summaries(raw: str) -> list[str]:
    out: list[str] = []
    for review in _field_list(raw, "reviews"):
        body = _body_of(review)
        if not body:
            continue  # an approval with no note carries nothing to act on
        state = str(review.get("state") or "").replace("_", " ").lower() or "review"
        out.append(f"[{state}] {_author_of(review)}:\n{body}")
    return out


def _inline_comments(raw: str) -> list[str]:
    """Inline review-thread comments, each tagged with the file and line it is anchored to.

    ``gh api --paginate`` concatenates pages as separate JSON arrays when a PR has many comments,
    so parse per-array and flatten rather than assuming one document.
    """
    out: list[str] = []
    for comment in _paginated_json_list(raw):
        body = _body_of(comment)
        if not body:
            continue
        path = comment.get("path")
        line = comment.get("line") or comment.get("original_line")
        where = f" @ {path}:{line}" if isinstance(path, str) and path else ""
        out.append(f"[inline] {_author_of(comment)}{where}:\n{body}")
    return out


def _paginated_json_list(raw: str) -> list[dict[str, Any]]:
    """Flatten ``gh api --paginate`` output, which may be several JSON arrays back to back."""
    items = _json_list(raw)
    if items or not raw.strip():
        return items
    # Multiple pages: gh emits "[...][...]"; split them apart and parse each.
    out: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = 0
    text = raw.strip()
    while index < len(text):
        try:
            value, offset = decoder.raw_decode(text, index)
        except ValueError:
            break
        if isinstance(value, list):
            out.extend(item for item in value if isinstance(item, dict))
        index = offset
        while index < len(text) and text[index].isspace():
            index += 1
    return out
