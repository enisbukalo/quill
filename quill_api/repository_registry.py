"""Cached discovery of GitHub repositories configured for Quill."""

from __future__ import annotations

import json
import base64
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from quill.config import CONFIG_FILENAME


#: How old a discovery snapshot may get before a read triggers a background rescan. Discovery
#: costs a handful of REST calls, so this can be short; the value only bounds how long a newly
#: added — or newly granted — repository stays invisible.
_CACHE_MAX_AGE_S = 300.0


class RepositoryScanError(RuntimeError):
    """GitHub repository discovery could not complete."""


@dataclass(frozen=True, slots=True)
class ConfiguredRepository:
    name: str
    visibility: str
    updated_at: str
    default_branch: str
    config_sha: str
    pr_review_enabled: bool = False
    project_board: str | None = None
    excluded_issue_labels: tuple[str, ...] = ()
    default_workflow: str = "ticket"
    pr_checks_required: bool = True


@dataclass(frozen=True, slots=True)
class _ConfigMetadata:
    pr_review_enabled: bool = False
    project_board: str | None = None
    excluded_issue_labels: tuple[str, ...] = ()
    default_workflow: str = "ticket"
    pr_checks_required: bool = True


class ConfiguredRepositoryRegistry:
    """Last complete scan of source repositories containing a root config file."""

    def __init__(
        self,
        cache_path: Path,
        on_refreshed: Callable[[], None] | None = None,
    ) -> None:
        self.cache_path = cache_path
        #: Called from the background refresh thread after a scan that changed the snapshot.
        #: The registry stays unaware of the event bus; the caller decides what to notify.
        self._on_refreshed = on_refreshed
        self._lock = threading.Lock()
        self._repositories: tuple[ConfiguredRepository, ...] = ()
        self._scanned_at: float | None = None
        self._error: str | None = None
        self._refresh_thread: threading.Thread | None = None
        self._load()

    @property
    def repositories(self) -> tuple[ConfiguredRepository, ...]:
        with self._lock:
            return self._repositories

    @property
    def scanned_at(self) -> float | None:
        with self._lock:
            return self._scanned_at

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def refresh(self) -> tuple[ConfiguredRepository, ...]:
        """Replace the cache only after a complete GitHub scan succeeds."""
        try:
            candidates = _accessible_repositories()
            found: list[ConfiguredRepository] = []
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(candidates)))) as pool:
                futures = {pool.submit(_configured, item): item for item in candidates}
                for future in as_completed(futures):
                    result = future.result()
                    if result is not None:
                        found.append(result)
            found.sort(key=lambda item: item.updated_at, reverse=True)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
            raise RepositoryScanError(str(exc)) from exc

        scanned_at = time.time()
        payload = {
            "scanned_at": scanned_at,
            "repositories": [asdict(item) for item in found],
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.cache_path)
        with self._lock:
            self._repositories = tuple(found)
            self._scanned_at = scanned_at
            self._error = None
        return tuple(found)

    def refresh_if_stale(self, max_age_s: float = _CACHE_MAX_AGE_S) -> None:
        """Start a background rescan when the snapshot has aged past ``max_age_s``.

        Cheap to call on every read: it only inspects a timestamp, and
        :meth:`refresh_async` already refuses to start a second concurrent scan.
        """
        with self._lock:
            scanned_at = self._scanned_at
        if scanned_at is not None and (time.time() - scanned_at) < max_age_s:
            return
        self.refresh_async()

    def refresh_async(self) -> None:
        """Refresh in the background while immediately serving the persisted snapshot."""
        with self._lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            thread = threading.Thread(target=self._refresh_safely, daemon=True)
            self._refresh_thread = thread
        thread.start()

    def _refresh_safely(self) -> None:
        """Rescan in the background and announce a snapshot that actually changed.

        Reads serve the cached snapshot immediately, so without this notification a client that
        fetched during a cold or stale window kept the old list until it happened to refetch.
        The queue page in particular selects its repository from ``project_board`` metadata, so
        an empty first response left it with no selectable repository and an empty table.

        Only a changed snapshot notifies: a periodic rescan that finds nothing new must not make
        every connected client refetch.
        """
        before = self.repositories
        try:
            self.refresh()
        except RepositoryScanError:
            return
        if self._on_refreshed is None or self.repositories == before:
            return
        try:
            self._on_refreshed()
        except Exception:  # noqa: BLE001 - a listener must never kill the refresh thread
            pass

    def _load(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            items = payload.get("repositories", [])
            repositories = tuple(
                _cached_repository(item) for item in items if isinstance(item, dict)
            )
            scanned_at = payload.get("scanned_at")
        except (OSError, ValueError, TypeError, KeyError):
            return
        with self._lock:
            self._repositories = repositories
            self._scanned_at = float(scanned_at) if isinstance(scanned_at, (int, float)) else None


def _accessible_repositories() -> list[dict[str, Any]]:
    """Every non-fork, non-archived repository the authenticated token can reach.

    Deliberately ``affiliation=owner,collaborator`` over ``gh repo list <login>``: that command
    lists repositories a login *owns*, so a service authenticating as a dedicated automation
    account — which reaches its targets as a collaborator, never as the owner — discovers
    nothing at all. Using REST also keeps discovery off the GraphQL quota, which the project
    board watcher already spends heavily.

    The result is normalised to the field names :func:`_configured` reads, so the shape does not
    depend on which GitHub API supplied it.
    """
    raw = _gh("api", "user/repos?affiliation=owner,collaborator&per_page=100")
    if not isinstance(raw, list):
        raise RepositoryScanError("GitHub repository list is invalid")
    candidates: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item = cast(dict[str, Any], entry)
        if item.get("fork") or item.get("archived"):
            continue
        branch = item.get("default_branch")
        candidates.append(
            {
                "nameWithOwner": item.get("full_name"),
                "visibility": item.get("visibility", ""),
                "updatedAt": item.get("updated_at", ""),
                "defaultBranchRef": {"name": branch} if isinstance(branch, str) else None,
            }
        )
    return candidates


def _configured(item: dict[str, Any]) -> ConfiguredRepository | None:
    name = item.get("nameWithOwner")
    default = item.get("defaultBranchRef")
    branch = default.get("name") if isinstance(default, dict) else None
    if not isinstance(name, str) or not isinstance(branch, str):
        return None
    try:
        content = _gh(
            "api", f"repos/{name}/contents/{CONFIG_FILENAME}", "-X", "GET", "-f", f"ref={branch}"
        )
    except RepositoryScanError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    if not isinstance(content, dict) or content.get("type") != "file":
        return None
    content_data = cast(dict[str, Any], content)
    metadata = _config_metadata(content_data)
    return ConfiguredRepository(
        name=name,
        visibility=str(item.get("visibility", "")),
        updated_at=str(item.get("updatedAt", "")),
        default_branch=branch,
        config_sha=str(content_data.get("sha", "")),
        pr_review_enabled=metadata.pr_review_enabled,
        project_board=metadata.project_board,
        excluded_issue_labels=metadata.excluded_issue_labels,
        default_workflow=metadata.default_workflow,
        pr_checks_required=metadata.pr_checks_required,
    )


def _has_pr_review(content: dict[str, Any]) -> bool:
    return _config_metadata(content).pr_review_enabled


def _config_metadata(content: dict[str, Any]) -> _ConfigMetadata:
    """Decode queue and workflow metadata from one remote root config file."""
    encoded = content.get("content")
    if not isinstance(encoded, str):
        return _ConfigMetadata()
    try:
        parsed = tomllib.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _ConfigMetadata()

    repo = parsed.get("repo")
    repo = repo if isinstance(repo, dict) else {}
    raw_board = repo.get("project_board")
    project_board = raw_board.strip() if isinstance(raw_board, str) and raw_board.strip() else None
    raw_excluded = repo.get("excluded_issue_labels")
    excluded_issue_labels = (
        tuple(
            dict.fromkeys(
                label.strip().casefold()
                for label in raw_excluded
                if isinstance(label, str) and label.strip()
            )
        )
        if isinstance(raw_excluded, list)
        else ()
    )
    workflows = parsed.get("workflows")
    review = workflows.get("pr_review") if isinstance(workflows, dict) else None
    raw_default = workflows.get("default") if isinstance(workflows, dict) else None
    default_workflow = (
        raw_default.strip() if isinstance(raw_default, str) and raw_default.strip() else "ticket"
    )
    return _ConfigMetadata(
        pr_review_enabled=isinstance(review, dict) and review.get("mode") == "review",
        project_board=project_board,
        excluded_issue_labels=excluded_issue_labels,
        default_workflow=default_workflow,
        pr_checks_required=repo.get("pr_checks_required") is not False,
    )


def _cached_repository(item: dict[str, Any]) -> ConfiguredRepository:
    """Load current and legacy persisted repository rows without losing the cache."""
    excluded = item.get("excluded_issue_labels", ())
    return ConfiguredRepository(
        name=str(item["name"]),
        visibility=str(item["visibility"]),
        updated_at=str(item["updated_at"]),
        default_branch=str(item["default_branch"]),
        config_sha=str(item["config_sha"]),
        pr_review_enabled=item.get("pr_review_enabled") is True,
        project_board=(
            value.strip()
            if isinstance((value := item.get("project_board")), str) and value.strip()
            else None
        ),
        excluded_issue_labels=tuple(
            label.casefold() for label in excluded if isinstance(label, str) and label.strip()
        )
        if isinstance(excluded, (list, tuple))
        else (),
        default_workflow=(
            workflow.strip()
            if isinstance((workflow := item.get("default_workflow")), str) and workflow.strip()
            else "ticket"
        ),
        pr_checks_required=item.get("pr_checks_required") is not False,
    )


def _gh(*args: str) -> object:
    try:
        completed = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryScanError(f"GitHub CLI request failed: {exc}") from exc
    if completed.returncode != 0:
        raise RepositoryScanError(completed.stderr.strip() or "GitHub CLI request failed")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RepositoryScanError("GitHub CLI returned invalid JSON") from exc
