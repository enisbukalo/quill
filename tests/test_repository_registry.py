from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from quill_api.repository_registry import (
    ConfiguredRepositoryRegistry,
    _config_metadata,
    _configured,
)


def _encoded(content: str) -> dict[str, str]:
    return {"content": base64.b64encode(content.encode()).decode(), "type": "file", "sha": "a"}


def test_config_metadata_decodes_project_queue_and_default_workflow() -> None:
    metadata = _config_metadata(
        _encoded(
            """
[repo]
project_board = " Board "
excluded_issue_labels = ["EPIC", "blocked", "epic"]
pr_checks_required = false

[workflows]
default = "feature"

[workflows.pr_review]
mode = "review"
"""
        )
    )

    assert metadata.project_board == "Board"
    assert metadata.excluded_issue_labels == ("epic", "blocked")
    assert metadata.default_workflow == "feature"
    assert metadata.pr_review_enabled
    assert metadata.pr_checks_required is False


def test_invalid_remote_config_keeps_safe_metadata_defaults() -> None:
    metadata = _config_metadata(_encoded("not = [valid"))

    assert metadata.project_board is None
    assert metadata.excluded_issue_labels == ()
    assert metadata.default_workflow == "ticket"
    assert not metadata.pr_review_enabled
    assert metadata.pr_checks_required is True


def test_configured_caches_remote_metadata_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from quill_api import repository_registry

    calls: list[tuple[str, ...]] = []
    content = _encoded(
        '[repo]\nproject_board="Board"\nexcluded_issue_labels=["EPIC"]\n'
        "pr_checks_required=false\n"
        '[workflows]\ndefault="ticket"\n[workflows.pr_review]\nmode="review"\n'
    )

    def fake_gh(*args: str) -> object:
        calls.append(args)
        return content

    monkeypatch.setattr(repository_registry, "_gh", fake_gh)
    result = _configured(
        {
            "nameWithOwner": "me/repo",
            "visibility": "PRIVATE",
            "updatedAt": "now",
            "defaultBranchRef": {"name": "main"},
        }
    )

    assert result is not None
    assert result.project_board == "Board"
    assert result.excluded_issue_labels == ("epic",)
    assert result.default_workflow == "ticket"
    assert result.pr_review_enabled
    assert result.pr_checks_required is False
    assert len(calls) == 1


def test_legacy_cache_loads_new_fields_with_defaults(tmp_path: Path) -> None:
    cache = tmp_path / "repositories.json"
    cache.write_text(
        json.dumps(
            {
                "scanned_at": 42,
                "repositories": [
                    {
                        "name": "me/legacy",
                        "visibility": "PRIVATE",
                        "updated_at": "old",
                        "default_branch": "main",
                        "config_sha": "abc",
                        "pr_review_enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = ConfiguredRepositoryRegistry(cache)

    assert registry.scanned_at == 42
    assert len(registry.repositories) == 1
    repository = registry.repositories[0]
    assert repository.name == "me/legacy"
    assert repository.project_board is None
    assert repository.excluded_issue_labels == ()
    assert repository.default_workflow == "ticket"
    assert repository.pr_checks_required is True


def test_current_cache_normalizes_collection_fields(tmp_path: Path) -> None:
    cache = tmp_path / "repositories.json"
    cache.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "me/current",
                        "visibility": "PUBLIC",
                        "updated_at": "now",
                        "default_branch": "trunk",
                        "config_sha": "def",
                        "project_board": " Board ",
                        "excluded_issue_labels": ["EPIC", "Blocked"],
                        "default_workflow": " feature ",
                        "pr_checks_required": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    repository = ConfiguredRepositoryRegistry(cache).repositories[0]

    assert repository.project_board == "Board"
    assert repository.excluded_issue_labels == ("epic", "blocked")
    assert repository.default_workflow == "feature"
    assert repository.pr_checks_required is False


def test_discovery_includes_repositories_reached_as_a_collaborator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that hid every repo from the runs tab.

    A dedicated automation account owns nothing — it reaches its targets as a collaborator. The
    previous ``gh repo list <login>`` listed only *owned* repositories, so discovery silently
    returned an empty set the moment the service authenticated as that account.
    """
    from quill_api import repository_registry

    captured: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> object:
        captured.append(args)
        return [
            {
                "full_name": "owner/collaborated",
                "visibility": "public",
                "updated_at": "2026-01-01T00:00:00Z",
                "default_branch": "main",
                "fork": False,
                "archived": False,
            }
        ]

    monkeypatch.setattr(repository_registry, "_gh", fake_gh)
    candidates = repository_registry._accessible_repositories()

    assert "affiliation=owner,collaborator" in captured[0][1]
    assert not any(arg == "list" for args in captured for arg in args), "must not use gh repo list"
    assert candidates == [
        {
            "nameWithOwner": "owner/collaborated",
            "visibility": "public",
            "updatedAt": "2026-01-01T00:00:00Z",
            "defaultBranchRef": {"name": "main"},
        }
    ]


def test_discovery_skips_forks_and_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from quill_api import repository_registry

    def fake_gh(*_args: str) -> object:
        return [
            {"full_name": "o/fork", "default_branch": "main", "fork": True, "archived": False},
            {"full_name": "o/old", "default_branch": "main", "fork": False, "archived": True},
            {"full_name": "o/live", "default_branch": "main", "fork": False, "archived": False},
        ]

    monkeypatch.setattr(repository_registry, "_gh", fake_gh)
    names = [c["nameWithOwner"] for c in repository_registry._accessible_repositories()]

    assert names == ["o/live"]


def test_stale_snapshot_triggers_a_background_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery ran only at service start, so a repository added — or access to one granted —
    afterwards stayed invisible until someone restarted the service."""
    from quill_api import repository_registry

    registry = repository_registry.ConfiguredRepositoryRegistry(tmp_path / "cache.json")
    started: list[bool] = []
    monkeypatch.setattr(registry, "refresh_async", lambda: started.append(True))

    registry._scanned_at = time.time() - 10_000
    registry.refresh_if_stale()
    assert started == [True], "an old snapshot must kick off a rescan"


def test_fresh_snapshot_does_not_rescan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The read path runs on every UI poll; it must not hammer GitHub."""
    from quill_api import repository_registry

    registry = repository_registry.ConfiguredRepositoryRegistry(tmp_path / "cache.json")
    started: list[bool] = []
    monkeypatch.setattr(registry, "refresh_async", lambda: started.append(True))

    registry._scanned_at = time.time()
    registry.refresh_if_stale()
    assert started == [], "a fresh snapshot must be served as-is"


def test_never_scanned_registry_rescans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quill_api import repository_registry

    registry = repository_registry.ConfiguredRepositoryRegistry(tmp_path / "cache.json")
    started: list[bool] = []
    monkeypatch.setattr(registry, "refresh_async", lambda: started.append(True))

    registry._scanned_at = None
    registry.refresh_if_stale()
    assert started == [True]
