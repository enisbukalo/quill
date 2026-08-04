from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quill.project_board import (
    ProjectCatalog,
    ProjectIssueGroup,
    ProjectIssueItem,
    ProjectMetadata,
    ProjectStatusOption,
)
from quill_api.app import create_app
from quill_api.repository_registry import ConfiguredRepository
from quill_api.schemas import ProjectQueueBatchResult, ProjectQueueAddResult
from quill_api.services import Services
from quill_api.settings import Settings


def _services(tmp_path: Path) -> Services:
    state = tmp_path / "state"
    services = Services(
        Settings(
            state_dir=state,
            workspace_root=state / "workspaces",
            runs_root=state / "runs",
            personas_root=tmp_path / "personas",
            skills_root=tmp_path / "skills",
            db_url="sqlite+pysqlite:///:memory:",
            vllm_url="http://vllm.example:8000",
            project_queue_watch_enabled=False,
        )
    )
    services.repositories._repositories = (
        ConfiguredRepository(
            "me/game",
            "PRIVATE",
            "now",
            "main",
            "abc",
            project_board="Game",
        ),
    )
    return services


def _catalog() -> ProjectCatalog:
    return ProjectCatalog(
        ProjectMetadata(
            "me",
            "Game",
            1,
            "project",
            "status",
            (ProjectStatusOption("queue", "Queue"),),
        ),
        (
            ProjectIssueGroup(
                4,
                "Foundation",
                (
                    ProjectIssueItem(
                        "item-13",
                        "me/game",
                        13,
                        "Create runtime",
                        ("enhancement",),
                        "Backlog",
                        4,
                        "Foundation",
                    ),
                ),
            ),
        ),
    )


def test_project_queue_rest_and_sync_share_empty_snapshot(tmp_path: Path) -> None:
    services = _services(tmp_path)
    client = TestClient(create_app(services))

    assert client.get("/project-queue").json() == {"batches": [], "depth": 0}
    assert services.live_sync()["project_queue"] == {"batches": [], "depth": 0}


def test_candidates_group_tickets_without_queueing_epic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(services.project_queue, "catalog", lambda _repo: _catalog())
    client = TestClient(create_app(services))

    response = client.get("/project-queue/me/game/candidates")

    assert response.status_code == 200
    assert response.json()["groups"] == [
        {
            "epic_number": 4,
            "epic_title": "Foundation",
            "tickets": [
                {
                    "number": 13,
                    "title": "Create runtime",
                    "labels": ["enhancement"],
                    "status": "Backlog",
                    "selectable": True,
                    "reason": None,
                }
            ],
        }
    ]


def test_batch_post_returns_per_ticket_partial_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(
        services.project_queue,
        "add_batch",
        lambda _repo, _tickets: ProjectQueueBatchResult(
            batch_id="batch-1",
            results=[
                ProjectQueueAddResult(ticket=3, queued=True),
                ProjectQueueAddResult(ticket=16, queued=False, reason="denied"),
            ],
        ),
    )
    client = TestClient(create_app(services))

    response = client.post("/project-queue/me/game", json={"tickets": [3, 16]})

    assert response.status_code == 202
    assert response.json()["batch_id"] == "batch-1"
    assert response.json()["results"][1] == {
        "ticket": 16,
        "queued": False,
        "reason": "denied",
    }


def test_batch_post_rejects_duplicate_tickets_before_github(tmp_path: Path) -> None:
    services = _services(tmp_path)
    client = TestClient(create_app(services))

    response = client.post("/project-queue/me/game", json={"tickets": [3, 3]})

    assert response.status_code == 422
