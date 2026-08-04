"""Cached discovery of GitHub repositories configured for Quill."""

from __future__ import annotations

import json
import base64
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from quill.config import CONFIG_FILENAME


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


@dataclass(frozen=True, slots=True)
class _ConfigMetadata:
    pr_review_enabled: bool = False
    project_board: str | None = None
    excluded_issue_labels: tuple[str, ...] = ()
    default_workflow: str = "ticket"


class ConfiguredRepositoryRegistry:
    """Last complete scan of source repositories containing a root config file."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
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
            viewer = _gh("api", "user")
            login = viewer.get("login") if isinstance(viewer, dict) else None
            if not isinstance(login, str) or not login:
                raise RepositoryScanError("GitHub user response is missing login")
            raw = _gh(
                "repo",
                "list",
                login,
                "--source",
                "--no-archived",
                "--limit",
                "100",
                "--json",
                "nameWithOwner,visibility,updatedAt,defaultBranchRef",
            )
            if not isinstance(raw, list):
                raise RepositoryScanError("GitHub repository list is invalid")
            candidates: list[dict[str, Any]] = [
                cast(dict[str, Any], item) for item in raw if isinstance(item, dict)
            ]
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

    def refresh_async(self) -> None:
        """Refresh in the background while immediately serving the persisted snapshot."""
        with self._lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            thread = threading.Thread(target=self._refresh_safely, daemon=True)
            self._refresh_thread = thread
        thread.start()

    def _refresh_safely(self) -> None:
        try:
            self.refresh()
        except RepositoryScanError:
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
