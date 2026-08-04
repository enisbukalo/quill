"""GitOps tests — GitHub metadata, delivery invariants, and subprocess decoding.

Branches, commits, pushes, and PR creation remain agent concerns. The driver reads ticket and PR
metadata, flattens update feedback, and repairs a missing closing-ticket reference after PR creation.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

import pytest

from quill.git_ops import (
    AmbiguousPullRequest,
    GitError,
    GitOps,
    PullRequest,
    SubprocessRunner,
    pr_target_for_repo,
)


class RecordingRunner:
    """Records every command; returns a queued/default stdout."""

    def __init__(self, returns: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._returns = returns or {}

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, val in self._returns.items():
            if key in joined:
                return val
        return ""


def test_subprocess_runner_decodes_utf8_not_cp1252() -> None:
    """Regression: gh/git output is UTF-8; a body with a cp1252-undefined byte must not crash.

    Ticket #130's body carried byte 0x90 (undefined in cp1252). With Windows' default cp1252
    decoding this raised UnicodeDecodeError in the subprocess reader thread, so `gh issue view`
    returned empty and the run failed with 'ticket has no fetchable body'. SubprocessRunner now
    forces encoding='utf-8', errors='replace'. Drive a real subprocess that writes UTF-8 box-drawing
    chars plus a raw 0x90 byte to stdout and assert it comes back decoded (0x90 -> U+FFFD), no raise.
    """
    # Write raw bytes to the stdout buffer: valid UTF-8 (─ = e2 94 80) plus a lone 0x90.
    prog = r"import sys; sys.stdout.buffer.write(b'\xe2\x94\x80 temp \x90 end')"
    runner = SubprocessRunner(directory=".")
    out = runner([sys.executable, "-c", prog])
    assert "temp" in out
    assert "─" in out  # the box-drawing dash decoded
    assert "�" in out  # the stray 0x90 became the replacement char, not a crash


def test_issue_body_calls_gh() -> None:
    runner = RecordingRunner(returns={"issue view": '{"title":"T","body":"B"}'})
    ops = GitOps(run=runner)
    out = ops.issue_body(130)
    assert out == '{"title":"T","body":"B"}'
    assert runner.calls == [["gh", "issue", "view", "130", "--json", "title,body"]]


def test_closing_ticket_reference_is_left_unchanged_when_github_resolved_it() -> None:
    runner = RecordingRunner(
        returns={
            "body,closingIssuesReferences": json.dumps(
                {"body": "Summary\n\nCloses #33", "closingIssuesReferences": [{"number": 33}]}
            )
        }
    )

    action = GitOps(runner).ensure_pr_closes_ticket(7, 33)

    assert action == "closing ticket already linked"
    assert len(runner.calls) == 1


def test_missing_closing_ticket_reference_is_appended_and_verified() -> None:
    class _Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.linked = False

        def __call__(self, args: Sequence[str]) -> str:
            call = list(args)
            self.calls.append(call)
            if call[:3] == ["gh", "pr", "view"]:
                references = [{"number": 33}] if self.linked else []
                body = "Summary\n\nCloses #33" if self.linked else "Summary"
                return json.dumps({"body": body, "closingIssuesReferences": references})
            if call[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/7"]:
                self.linked = True
                return "{}"
            return ""

    runner = _Runner()

    action = GitOps(runner).ensure_pr_closes_ticket(7, 33)

    assert action == "added missing closing-ticket reference"
    patch_call = runner.calls[1]
    assert patch_call[:5] == [
        "gh",
        "api",
        "repos/{owner}/{repo}/pulls/7",
        "--method",
        "PATCH",
    ]
    assert patch_call[-1] == "body=Summary\n\nCloses #33"


def test_closing_ticket_repair_retries_github_metadata_propagation(monkeypatch) -> None:
    class _Runner:
        def __init__(self) -> None:
            self.views = 0

        def __call__(self, args: Sequence[str]) -> str:
            call = list(args)
            if call[:3] == ["gh", "pr", "view"]:
                self.views += 1
                references = [{"number": 33}] if self.views >= 4 else []
                return json.dumps({"body": "Summary", "closingIssuesReferences": references})
            return "{}"

    sleeps: list[float] = []
    monkeypatch.setattr("quill.git_ops._sleep", sleeps.append)

    action = GitOps(_Runner()).ensure_pr_closes_ticket(7, 33)

    assert action == "added missing closing-ticket reference"
    assert sleeps == [1.0, 1.0]


def test_closing_ticket_repair_fails_when_github_does_not_resolve_it(monkeypatch) -> None:
    runner = RecordingRunner(
        returns={
            "body,closingIssuesReferences": json.dumps(
                {"body": "Summary", "closingIssuesReferences": []}
            )
        }
    )
    monkeypatch.setattr("quill.git_ops._sleep", lambda _seconds: None)

    with pytest.raises(GitError, match="still does not close ticket #33"):
        GitOps(runner).ensure_pr_closes_ticket(7, 33)


def test_managed_pr_comment_updates_existing_comment() -> None:
    marker = "<!-- quill-pr-review -->"
    runner = RecordingRunner(
        returns={"issues/7/comments": json.dumps([{"id": 91, "body": f"{marker}\nold"}])}
    )

    action = GitOps(runner).upsert_pr_comment(7, marker, f"{marker}\nnew")

    assert action == "updated"
    assert runner.calls[-1][:4] == [
        "gh",
        "api",
        "repos/{owner}/{repo}/issues/comments/91",
        "--method",
    ]


def test_managed_pr_comment_creates_when_rest_list_has_no_marker() -> None:
    marker = "<!-- quill-pr-review -->"
    runner = RecordingRunner(returns={"issues/7/comments": "[]"})

    action = GitOps(runner).upsert_pr_comment(7, marker, f"{marker}\nnew")

    assert action == "created"
    assert runner.calls == [
        ["gh", "api", "--paginate", "repos/{owner}/{repo}/issues/7/comments"],
        ["gh", "pr", "comment", "7", "--body", f"{marker}\nnew"],
    ]


def test_managed_pr_comment_rejects_duplicate_rest_markers() -> None:
    marker = "<!-- quill-pr-review -->"
    runner = RecordingRunner(
        returns={
            "issues/7/comments": json.dumps(
                [{"id": 91, "body": marker}, {"id": 92, "body": marker}]
            )
        }
    )

    with pytest.raises(GitError, match="multiple managed comments"):
        GitOps(runner).upsert_pr_comment(7, marker, marker)


@pytest.mark.parametrize("comment_id", [None, "91", True])
def test_managed_pr_comment_requires_numeric_rest_id(comment_id: object) -> None:
    marker = "<!-- quill-pr-review -->"
    runner = RecordingRunner(
        returns={"issues/7/comments": json.dumps([{"id": comment_id, "body": marker}])}
    )

    with pytest.raises(GitError, match="no numeric database ID"):
        GitOps(runner).upsert_pr_comment(7, marker, marker)


# -- update mode: finding the ticket's open PR ------------------------------------


def _pr_list(*entries: dict[str, object]) -> str:
    return json.dumps(list(entries))


def test_pr_for_ticket_searches_open_prs() -> None:
    runner = RecordingRunner(
        returns={
            "pr view": json.dumps(
                {"headRefOid": "abc123", "commits": [{"committedDate": "2026-07-01T00:00:00Z"}]}
            ),
            "pr list": _pr_list(
                {
                    "number": 34,
                    "headRefName": "ticket-33-engine",
                    "title": "Data-driven engine (closes #33)",
                    "body": "b",
                    "url": "https://github.com/me/proj/pull/34",
                }
            ),
        }
    )
    pr = GitOps(run=runner).pr_target_for_ticket(33)
    assert pr is not None
    assert (pr.number, pr.branch, pr.url) == (
        34,
        "ticket-33-engine",
        "https://github.com/me/proj/pull/34",
    )
    assert runner.calls[0][:5] == ["gh", "pr", "list", "--state", "open"]
    assert "--search" in runner.calls[0]
    assert pr.head_sha == "abc123"


def test_remote_pr_target_resolves_without_a_checkout() -> None:
    runner = RecordingRunner(
        returns={
            "pr list": _pr_list(
                {
                    "number": 34,
                    "headRefName": "ticket-33-engine",
                    "title": "Fix #33",
                    "url": "u",
                }
            ),
            "pr view": json.dumps(
                {"headRefOid": "abc123", "commits": [{"committedDate": "2026-07-01T00:00:00Z"}]}
            ),
        }
    )

    pr = pr_target_for_repo(runner, "me/proj", 33)

    assert pr is not None and (pr.number, pr.head_sha) == (34, "abc123")
    assert all("--repo" in call for call in runner.calls)


def test_pr_for_ticket_rejects_ambiguous_matches() -> None:
    runner = RecordingRunner(
        returns={
            "pr list": _pr_list(
                {"number": 1, "headRefName": "ticket-33-a", "title": "#33", "url": "u"},
                {"number": 2, "headRefName": "ticket-33-b", "title": "#33", "url": "v"},
            )
        }
    )
    with pytest.raises(AmbiguousPullRequest, match="multiple open PRs"):
        GitOps(run=runner).pr_for_ticket(33)


def test_feedback_snapshot_uses_strict_commit_boundary_and_edits() -> None:
    pr = PullRequest(34, "b", "T", "u", "abc", "2026-07-01T12:00:00Z")
    runner = ScriptedRunner(
        {
            "comments,reviews": json.dumps(
                {
                    "comments": [
                        {"id": "old", "body": "old", "updatedAt": "2026-07-01T12:00:00Z"},
                        {
                            "id": "edit",
                            "body": "edited",
                            "createdAt": "2026-06-01T00:00:00Z",
                            "updatedAt": "2026-07-01T12:00:01Z",
                        },
                    ],
                    "reviews": [
                        {"id": "review", "body": "new", "submittedAt": "2026-07-01T12:00:02+00:00"}
                    ],
                }
            ),
            "gh api": "[]",
        }
    )
    snapshot = GitOps(runner).feedback_snapshot(pr)
    assert [item.id for item in snapshot.selected] == ["edit", "review"]
    assert snapshot.excluded_old == 1


def test_pr_for_ticket_returns_none_when_no_open_pr() -> None:
    runner = RecordingRunner(returns={"pr list": "[]"})
    assert GitOps(run=runner).pr_for_ticket(33) is None


def test_pr_for_branch_uses_exact_head_without_search_index() -> None:
    runner = RecordingRunner(
        returns={
            "pr list": json.dumps(
                [
                    {
                        "number": 41,
                        "headRefName": "ticket-33-fix",
                        "title": "Fix it",
                        "body": "",
                        "url": "https://example/pr/41",
                    }
                ]
            )
        }
    )

    pr = GitOps(run=runner).pr_for_branch("ticket-33-fix")

    assert pr is not None and pr.number == 41
    assert "--head" in runner.calls[0]
    assert "--search" not in runner.calls[0]


def test_pr_for_ticket_ignores_unparseable_output() -> None:
    """A gh read that returns junk degrades to 'no PR', never a traceback mid-run."""
    runner = RecordingRunner(returns={"pr list": "not json"})
    assert GitOps(run=runner).pr_for_ticket(33) is None


def test_pr_for_ticket_does_not_match_a_longer_number() -> None:
    """`--search 33` is full-text, so PR #330's branch would match a naive substring check.

    Ticket 33 must not hijack a PR that only mentions 330 — that would check out the wrong branch
    and revise unrelated work.
    """
    runner = RecordingRunner(
        returns={
            "pr list": _pr_list(
                {
                    "number": 99,
                    "headRefName": "ticket-330-other",
                    "title": "Something for 330",
                    "body": "",
                    "url": "u",
                }
            )
        }
    )
    assert GitOps(run=runner).pr_for_ticket(33) is None


# -- update mode: flattening PR feedback ------------------------------------------


class ScriptedRunner:
    """Returns a canned stdout per command substring; records the calls."""

    def __init__(self, returns: dict[str, str]) -> None:
        self.calls: list[list[str]] = []
        self._returns = returns

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, val in self._returns.items():
            if key in joined:
                return val
        return ""


def test_pr_feedback_merges_all_three_comment_sources() -> None:
    """Top-level comments, review summaries, and inline thread comments all reach the prompt.

    No single gh call returns all three: `--json comments` omits review summaries AND inline
    comments, so a run that read only that would silently drop the most actionable feedback.
    """
    runner = ScriptedRunner(
        {
            "--json comments": json.dumps(
                {"comments": [{"author": {"login": "ann"}, "body": "top-level note"}]}
            ),
            "--json reviews": json.dumps(
                {
                    "reviews": [
                        {
                            "author": {"login": "bob"},
                            "state": "CHANGES_REQUESTED",
                            "body": "needs work",
                        },
                        {"author": {"login": "cat"}, "state": "APPROVED", "body": ""},
                    ]
                }
            ),
            "gh api": json.dumps(
                [
                    {
                        "user": {"login": "dan"},
                        "body": "rename this",
                        "path": "quill/engine.py",
                        "line": 42,
                    }
                ]
            ),
        }
    )
    out = GitOps(run=runner).pr_feedback(34)

    assert "top-level note" in out
    assert "needs work" in out
    assert "changes requested" in out  # the review state is labelled
    assert "rename this" in out
    assert "quill/engine.py:42" in out  # inline comments carry their anchor
    assert "cat" not in out  # a bodyless approval adds nothing to act on


def test_pr_feedback_uses_rest_api_for_inline_comments() -> None:
    """Inline comments need the REST endpoint — `gh pr view --json` has no shape for them."""
    runner = ScriptedRunner({})
    GitOps(run=runner).pr_feedback(34)
    joined = [" ".join(c) for c in runner.calls]
    assert any("pulls/34/comments" in c and "gh api" in c for c in joined)


def test_pr_feedback_empty_when_pr_has_no_comments() -> None:
    runner = ScriptedRunner(
        {"--json comments": '{"comments":[]}', "--json reviews": '{"reviews":[]}'}
    )
    assert GitOps(run=runner).pr_feedback(34) == ""


def test_pr_feedback_handles_paginated_inline_comments() -> None:
    """`gh api --paginate` emits one JSON array per page; both pages must be read."""
    page1 = json.dumps([{"user": {"login": "a"}, "body": "first", "path": "f.py", "line": 1}])
    page2 = json.dumps([{"user": {"login": "b"}, "body": "second", "path": "g.py", "line": 2}])
    runner = ScriptedRunner({"gh api": page1 + page2})
    out = GitOps(run=runner).pr_feedback(34)
    assert "first" in out
    assert "second" in out


# -- CI checks --------------------------------------------------------------------


def _rollup(*entries: dict[str, object]) -> str:
    return json.dumps({"statusCheckRollup": list(entries)})


def _merge_metadata(**overrides: object) -> str:
    data: dict[str, object] = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefName": "feature/ticket-33",
        "headRefOid": "abc123",
        "baseRefName": "main",
        "statusCheckRollup": [
            {
                "name": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
    }
    data.update(overrides)
    return json.dumps(data)


def _merged_metadata() -> str:
    return json.dumps(
        {
            "state": "MERGED",
            "mergedAt": "2026-08-01T21:00:00Z",
            "mergeCommit": {"oid": "merge456"},
            "headRefName": "feature/ticket-33",
            "headRefOid": "abc123",
            "baseRefName": "main",
        }
    )


def test_reviewed_pr_merge_is_sha_guarded_and_deletes_only_remote_branch() -> None:
    class _Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.remote_exists = True

        def __call__(self, args: Sequence[str]) -> str:
            call = list(args)
            self.calls.append(call)
            joined = " ".join(call)
            if "number,state,isDraft" in joined:
                return _merge_metadata()
            if "state,mergedAt,mergeCommit" in joined:
                return _merged_metadata()
            if call[:3] == ["git", "ls-remote", "--heads"]:
                return "abc123\trefs/heads/feature/ticket-33" if self.remote_exists else ""
            if call[:3] == ["git", "push", "origin"]:
                self.remote_exists = False
            return ""

    runner = _Runner()

    action = GitOps(runner).merge_reviewed_pr(
        7,
        expected_head_sha="abc123",
        expected_branch="feature/ticket-33",
        expected_base="main",
    )

    assert action == "merged PR #7 and deleted remote branch feature/ticket-33"
    assert ["gh", "pr", "merge", "7", "--merge", "--match-head-commit", "abc123"] in runner.calls
    assert ["git", "push", "origin", ":refs/heads/feature/ticket-33"] in runner.calls
    assert not any(call[:2] == ["git", "branch"] for call in runner.calls)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"headRefOid": "moved"}, "moved from"),
        ({"isDraft": True}, "still a draft"),
        ({"mergeStateStatus": "BLOCKED"}, "not CLEAN"),
        ({"statusCheckRollup": []}, "no reported CI checks"),
        (
            {"statusCheckRollup": [{"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}]},
            "failed CI checks",
        ),
    ],
)
def test_reviewed_pr_merge_rejects_unsafe_or_unverified_state(
    overrides: dict[str, object], message: str
) -> None:
    runner = RecordingRunner({"number,state,isDraft": _merge_metadata(**overrides)})

    with pytest.raises(GitError, match=message):
        GitOps(runner).merge_reviewed_pr(
            7,
            expected_head_sha="abc123",
            expected_branch="feature/ticket-33",
            expected_base="main",
        )

    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_reviewed_pr_merge_tolerates_github_auto_deleting_remote_branch() -> None:
    runner = RecordingRunner(
        {
            "number,state,isDraft": _merge_metadata(),
            "state,mergedAt,mergeCommit": _merged_metadata(),
            "git ls-remote": "",
        }
    )

    action = GitOps(runner).merge_reviewed_pr(
        7,
        expected_head_sha="abc123",
        expected_branch="feature/ticket-33",
        expected_base="main",
    )

    assert "already deleted" in action
    assert not any(call[:3] == ["git", "push", "origin"] for call in runner.calls)


def test_pr_checks_normalises_check_runs() -> None:
    raw = _rollup(
        {
            "__typename": "CheckRun",
            "name": "build",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://github.com/me/p/actions/runs/99/job/1",
        },
        {"__typename": "CheckRun", "name": "test", "status": "IN_PROGRESS", "conclusion": None},
    )
    ops = GitOps(run=RecordingRunner({"gh pr view": raw}))

    status = ops.pr_checks(7)

    assert [c.state for c in status.checks] == ["pass", "pending"]
    assert not status.settled
    assert status.checks[0].run_id == "99"


def test_pr_checks_normalises_legacy_status_contexts() -> None:
    """The older commit-status API uses `context`/`state` instead of `name`/`conclusion`; a repo
    using it must not silently report zero checks."""
    raw = _rollup(
        {
            "__typename": "StatusContext",
            "context": "ci/jenkins",
            "state": "FAILURE",
            "targetUrl": "https://jenkins/job/1",
        },
        {"__typename": "StatusContext", "context": "ci/lint", "state": "SUCCESS"},
    )
    ops = GitOps(run=RecordingRunner({"gh pr view": raw}))

    status = ops.pr_checks(7)

    assert status.settled
    assert [c.name for c in status.failed] == ["ci/jenkins"]


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("SUCCESS", "pass"),
        ("SKIPPED", "pass"),  # a conditional job that opted out must not block forever
        ("NEUTRAL", "pass"),
        ("FAILURE", "fail"),
        ("TIMED_OUT", "fail"),
        ("CANCELLED", "fail"),
        ("ACTION_REQUIRED", "fail"),
    ],
)
def test_pr_checks_conclusion_mapping(conclusion: str, expected: str) -> None:
    raw = _rollup({"name": "c", "status": "COMPLETED", "conclusion": conclusion})
    ops = GitOps(run=RecordingRunner({"gh pr view": raw}))
    assert ops.pr_checks(7).checks[0].state == expected


def test_pr_checks_reports_nothing_when_no_checks_exist() -> None:
    """`reported` is what separates "CI has not started" from "CI is green" — conflating them
    would let a run walk through a gate whose workflows had not registered yet."""
    ops = GitOps(run=RecordingRunner({"gh pr view": _rollup()}))

    status = ops.pr_checks(7)

    assert not status.reported
    assert not status.settled


def test_pr_checks_uses_the_rollup_not_gh_pr_checks() -> None:
    """`gh pr checks` signals its verdict through the exit code, which SubprocessRunner turns into
    a GitError that throws away the output."""
    runner = RecordingRunner({"gh pr view": _rollup()})
    GitOps(run=runner).pr_checks(7)
    assert runner.calls[0] == ["gh", "pr", "view", "7", "--json", "statusCheckRollup"]


def test_pr_checks_survives_unparsable_output() -> None:
    assert GitOps(run=RecordingRunner({"gh": "not json"})).pr_checks(7).checks == ()


def test_failed_check_log_returns_empty_when_it_cannot_be_read() -> None:
    """The gate verdict is already decided; a log that won't download must not crash the run."""

    def boom(args: Sequence[str]) -> str:
        raise GitError(f"no such run: {' '.join(args)}")

    assert GitOps(run=boom).failed_check_log("99") == ""


def test_failed_check_log_keeps_a_precise_failed_step_log() -> None:
    """When the workflow fails loudly, --log-failed already names the cause: use it and do not pull
    the whole (potentially huge) run log."""
    detail = "test\tpytest\tE   AssertionError: expected 3 got 4\ntest\tpytest\tFAILED test_x.py::y"
    calls: list[str] = []

    def run(args: Sequence[str]) -> str:
        calls.append(" ".join(args))
        return detail if "--log-failed" in " ".join(args) else "SHOULD-NOT-BE-FETCHED"

    out = GitOps(run=run).failed_check_log("1")
    assert "AssertionError: expected 3 got 4" in out
    assert calls == ["gh run view 1 --log-failed"]  # the full run log was never fetched


def test_failed_check_log_falls_back_to_full_log_for_a_summary_gate() -> None:
    """A workflow that hides the failure behind a summary gate makes --log-failed a useless stub;
    the real test output lives in the full run log and must reach the revise agent."""
    stub = (
        "Run Tests\tReport CI result\tTEST_OUTCOME: failure\n"
        "Run Tests\tReport CI result\t##[error]Process completed with exit code 1"
    )
    full = "\n".join(
        [
            "build\tCompile\tBuild succeeded",
            "test\tctest\tThe following tests FAILED:",
            "test\tctest\t  5 - VllmMonitorTest.Throughput (Failed)",
            "test\tctest\tExpected prefill 42 but got 0",
            "report\tReport CI result\tTEST_OUTCOME: failure",
        ]
    )

    def run(args: Sequence[str]) -> str:
        joined = " ".join(args)
        if "--log-failed" in joined:
            return stub
        return full if "--log" in joined else ""

    out = GitOps(run=run).failed_check_log("30291922828")
    assert "VllmMonitorTest.Throughput (Failed)" in out
    assert "Expected prefill 42 but got 0" in out


def test_failed_check_log_is_size_bounded() -> None:
    """A revise prompt must not be swamped by a giant matrix log."""
    big = "\n".join(f"line {i} error boom" for i in range(5000))

    def run(args: Sequence[str]) -> str:
        return big if "--log-failed" in " ".join(args) else ""

    out = GitOps(run=run).failed_check_log("1")
    assert len(out) <= 16000 + 40  # budget plus the short truncation marker
    assert "truncated" in out
