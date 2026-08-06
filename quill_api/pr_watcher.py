"""Poll configured GitHub repositories and admit one review per eligible PR head."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from quill_api.repository_registry import ConfiguredRepository


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    repo: str
    branch: str
    ticket: int
    pr_number: int
    head_sha: str


type AdmitReview = Callable[[ReviewCandidate], bool]
type Maintain = Callable[[], None]


class RepositorySource(Protocol):
    @property
    def repositories(self) -> tuple[ConfiguredRepository, ...]: ...


class PullRequestWatcher:
    """Discover reviewable PR heads without owning run execution."""

    def __init__(
        self,
        repositories: RepositorySource,
        admit: AdmitReview,
        *,
        interval_s: float,
        enabled: bool = True,
        maintain: Maintain | None = None,
    ) -> None:
        self._repositories = repositories
        self._admit = admit
        self._interval_s = interval_s
        self._enabled = enabled
        self._maintain = maintain
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="quill-pr-watcher", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def scan_once(self) -> list[ReviewCandidate]:
        """Return candidates discovered this pass and offer each to the admission callback."""
        if self._maintain is not None:
            self._maintain()
        candidates: list[ReviewCandidate] = []
        for repository in self._repositories.repositories:
            if not repository.pr_review_enabled:
                continue
            for item in _open_pull_requests(repository.name):
                candidate = _candidate(
                    repository.name,
                    item,
                    pr_checks_required=repository.pr_checks_required,
                )
                if candidate is None:
                    continue
                candidates.append(candidate)
                self._admit(candidate)
        return candidates

    def _run(self) -> None:
        while not self._stopping.wait(self._interval_s):
            try:
                self.scan_once()
            except (OSError, ValueError, subprocess.SubprocessError):
                # GitHub availability is transient. The next bounded poll retries without taking
                # down the API or the run queue.
                continue


def _open_pull_requests(repo: str) -> list[dict[str, object]]:
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,headRefName,headRefOid,isDraft,statusCheckRollup,closingIssuesReferences,url",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or f"could not list pull requests for {repo}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list):
        raise ValueError(f"GitHub returned an invalid pull-request list for {repo}")
    return [item for item in payload if isinstance(item, dict)]


def _candidate(
    repo: str,
    item: dict[str, object],
    *,
    pr_checks_required: bool = True,
) -> ReviewCandidate | None:
    if item.get("isDraft") is True:
        return None
    checks = item.get("statusCheckRollup")
    if not isinstance(checks, list):
        return None
    if not checks and pr_checks_required:
        return None
    if checks and not all(_successful(check) for check in checks):
        return None
    issues = item.get("closingIssuesReferences")
    if not isinstance(issues, list):
        return None
    tickets = {
        number
        for issue in issues
        if isinstance(issue, dict) and isinstance((number := issue.get("number")), int)
    }
    if len(tickets) != 1:
        return None
    number = item.get("number")
    branch = item.get("headRefName")
    head_sha = item.get("headRefOid")
    if not isinstance(number, int) or not isinstance(branch, str) or not isinstance(head_sha, str):
        return None
    return ReviewCandidate(repo, branch, tickets.pop(), number, head_sha)


def _successful(check: object) -> bool:
    if not isinstance(check, dict):
        return False
    status = str(check.get("status") or "").upper()
    if status:
        conclusion = str(check.get("conclusion") or "").upper()
        return status == "COMPLETED" and conclusion == "SUCCESS"
    state = str(check.get("state") or "").upper()
    return state == "SUCCESS"
