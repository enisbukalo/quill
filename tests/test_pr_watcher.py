"""Automatic pull-request review discovery and admission."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from quill_api import pr_watcher
from quill_api.pr_watcher import PullRequestWatcher, ReviewCandidate
from quill_api.repository_registry import ConfiguredRepository, _has_pr_review


def _pr(*, state: str = "SUCCESS", draft: bool = False) -> dict[str, object]:
    return {
        "number": 12,
        "headRefName": "enhancement/example_42",
        "headRefOid": "abc123",
        "isDraft": draft,
        "statusCheckRollup": [{"state": state}],
        "closingIssuesReferences": [{"number": 42}],
    }


@pytest.mark.parametrize(
    "check",
    [
        {"state": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
    ],
)
def test_successful_check_results_create_review_candidate(check: dict[str, str]) -> None:
    candidate = pr_watcher._candidate(
        "me/repo",
        {**_pr(), "statusCheckRollup": [check]},
    )

    assert candidate == ReviewCandidate(
        repo="me/repo",
        branch="enhancement/example_42",
        ticket=42,
        pr_number=12,
        head_sha="abc123",
    )


@pytest.mark.parametrize(
    "check",
    [
        {"state": "PENDING"},
        {"state": "FAILURE"},
        {"state": "ERROR"},
        {"status": "IN_PROGRESS", "conclusion": ""},
        {"status": "COMPLETED", "conclusion": ""},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
        {"status": "COMPLETED", "conclusion": "TIMED_OUT"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
    ],
)
def test_non_successful_check_results_are_skipped(check: dict[str, str]) -> None:
    item = {**_pr(), "statusCheckRollup": [check]}

    assert pr_watcher._candidate("me/repo", item) is None


def test_mixed_check_results_are_skipped() -> None:
    item = {
        **_pr(),
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    }

    assert pr_watcher._candidate("me/repo", item) is None


def test_empty_check_rollup_is_admitted_only_when_repository_allows_it() -> None:
    item = {**_pr(), "statusCheckRollup": []}

    assert pr_watcher._candidate("me/repo", item) is None
    assert pr_watcher._candidate("me/repo", item, pr_checks_required=False) == ReviewCandidate(
        repo="me/repo",
        branch="enhancement/example_42",
        ticket=42,
        pr_number=12,
        head_sha="abc123",
    )


@pytest.mark.parametrize(
    "checks",
    [
        None,
        "invalid",
        [{"status": "IN_PROGRESS", "conclusion": ""}],
        [{"status": "COMPLETED", "conclusion": "FAILURE"}],
    ],
)
def test_optional_check_policy_still_rejects_malformed_or_unsuccessful_rollups(
    checks: object,
) -> None:
    item = {**_pr(), "statusCheckRollup": checks}

    assert pr_watcher._candidate("me/repo", item, pr_checks_required=False) is None


@pytest.mark.parametrize(
    "item",
    [
        _pr(state="PENDING"),
        _pr(draft=True),
        {**_pr(), "statusCheckRollup": []},
        {**_pr(), "closingIssuesReferences": []},
        {**_pr(), "closingIssuesReferences": [{"number": 1}, {"number": 2}]},
    ],
)
def test_ineligible_pull_requests_are_skipped(item: dict[str, object]) -> None:
    assert pr_watcher._candidate("me/repo", item) is None


def test_scan_uses_only_repositories_with_review_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = SimpleNamespace(
        repositories=(
            ConfiguredRepository("me/enabled", "PRIVATE", "now", "main", "a", True),
            ConfiguredRepository("me/disabled", "PRIVATE", "now", "main", "b", False),
        )
    )
    queried: list[str] = []
    admitted: list[ReviewCandidate] = []

    def listing(repo: str) -> list[dict[str, object]]:
        queried.append(repo)
        return [_pr()]

    def admit(candidate: ReviewCandidate) -> bool:
        admitted.append(candidate)
        return True

    monkeypatch.setattr(pr_watcher, "_open_pull_requests", listing)
    watcher = PullRequestWatcher(repositories, admit, interval_s=15)

    candidates = watcher.scan_once()

    assert queried == ["me/enabled"]
    assert candidates == admitted


def test_scan_applies_each_repository_check_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    repositories = SimpleNamespace(
        repositories=(
            ConfiguredRepository(
                "me/local",
                "PRIVATE",
                "now",
                "main",
                "a",
                pr_review_enabled=True,
                pr_checks_required=False,
            ),
        )
    )
    item = {**_pr(), "statusCheckRollup": []}
    monkeypatch.setattr(pr_watcher, "_open_pull_requests", lambda _repo: [item])
    admitted: list[ReviewCandidate] = []

    def admit(candidate: ReviewCandidate) -> bool:
        admitted.append(candidate)
        return True

    candidates = PullRequestWatcher(repositories, admit, interval_s=15).scan_once()

    assert candidates == admitted
    assert len(candidates) == 1


def test_scan_runs_feedback_maintenance_before_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    repositories = SimpleNamespace(
        repositories=(ConfiguredRepository("me/enabled", "PRIVATE", "now", "main", "a", True),)
    )
    monkeypatch.setattr(
        pr_watcher,
        "_open_pull_requests",
        lambda _repo: order.append("discover") or [],
    )
    watcher = PullRequestWatcher(
        repositories,
        lambda _candidate: True,
        interval_s=15,
        maintain=lambda: order.append("maintain"),
    )

    watcher.scan_once()

    assert order == ["maintain", "discover"]


def test_repository_config_declares_review_automation_eligibility() -> None:
    encoded = base64.b64encode(b'[workflows.pr_review]\nmode = "review"\n').decode("ascii")

    assert _has_pr_review({"content": encoded})
    assert not _has_pr_review({"content": base64.b64encode(b"[repo]\n").decode("ascii")})
