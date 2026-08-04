from __future__ import annotations

import base64
import json
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


def test_invalid_remote_config_keeps_safe_metadata_defaults() -> None:
    metadata = _config_metadata(_encoded("not = [valid"))

    assert metadata.project_board is None
    assert metadata.excluded_issue_labels == ()
    assert metadata.default_workflow == "ticket"
    assert not metadata.pr_review_enabled


def test_configured_caches_remote_metadata_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from quill_api import repository_registry

    calls: list[tuple[str, ...]] = []
    content = _encoded(
        '[repo]\nproject_board="Board"\nexcluded_issue_labels=["EPIC"]\n'
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
