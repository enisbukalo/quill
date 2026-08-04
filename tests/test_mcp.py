"""The MCP layer stays a bounded adapter over QuillClient."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any, Self

import pytest

from quill.client import RemoteRun
from quill_mcp import server


class FakeClient:
    last_start: dict[str, object] | None = None
    review_branch = "feature/review-target_7"

    def __init__(self, base: str) -> None:
        self.base = base

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def start(self, **kwargs: object) -> RemoteRun:
        type(self).last_start = kwargs
        return RemoteRun("r1", "queued", 2)

    def runs(self, **_kwargs: object) -> list[dict[str, Any]]:
        return [{"run_id": "r1", "status": "done", "repo": "me/proj", "ticket": 7}]

    def status(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": "done", "history": [{"phase": "ci"}]}

    def artifacts(self, _run_id: str) -> list[dict[str, Any]]:
        return [{"name": "plan.md", "size": 4}]

    def breakdown(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "phases": [{"tool_calls": [{"arguments": {"all": True}}]}]}

    def queue(self) -> dict[str, Any]:
        return {"active": None, "queued": [{"run_id": "r1", "status": "queued"}], "depth": 1}

    def review_target(self, _repo: str, _ticket: int) -> str:
        return self.review_branch

    def stop(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": "halted"}

    def answer(self, run_id: str, _answer: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": "running"}

    def restart(self, run_id: str, phase: str) -> dict[str, Any]:
        return {
            "run_id": f"{run_id}-restart",
            "status": "queued",
            "source_run_id": run_id,
            "start_phase": phase,
        }


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.last_start = None
    monkeypatch.setattr(server, "_client_factory", FakeClient)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/me/proj.git"],
        cwd=repo,
        check=True,
    )
    (repo / "quillfolio.toml").write_text('[runner]\nkind="pi"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=quill",
            "-c",
            "user.email=quill@test",
            "commit",
            "-m",
            "seed",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def test_start_returns_immediately_with_checkout_context(checkout: Path) -> None:
    result = server.quill_start(str(checkout), 7)
    assert result == {
        "run_id": "r1",
        "status": "queued",
        "queue_position": 2,
        "workflow": "ticket",
        "repo": "me/proj",
        "branch": "main",
        "ticket": 7,
        "mode": "create",
    }
    assert FakeClient.last_start is not None
    assert "clear_prefix_cache" not in FakeClient.last_start


def test_start_review_resolves_the_open_pr_branch(checkout: Path) -> None:
    result = server.quill_start(str(checkout), 7, mode="review", workflow="pr_review")

    assert result["branch"] == "feature/review-target_7"
    assert result["mode"] == "review"
    assert FakeClient.last_start == {
        "repo": "me/proj",
        "branch": "feature/review-target_7",
        "ticket": 7,
        "mode": "review",
        "workflow": "pr_review",
    }


def test_status_can_rediscover_and_add_artifacts(checkout: Path) -> None:
    result = server.quill_status(repo_path=str(checkout), ticket=7)
    assert result["run_id"] == "r1"
    assert result["history"] == [{"phase": "ci"}]
    assert result["artifacts"] == [{"name": "plan.md", "size": 4}]


def test_recent_queue_stop_and_answer(checkout: Path) -> None:
    assert server.quill_recent_runs(str(checkout), ticket=7)["runs"][0]["run_id"] == "r1"
    assert server.quill_queue()["depth"] == 1
    assert server.quill_stop("r1")["status"] == "halted"
    assert server.quill_answer("r1", "continue")["status"] == "running"
    assert server.quill_restart("r1", "impl")["status"] == "queued"
    assert server.quill_run_breakdown("r1")["phases"][0]["tool_calls"][0]["arguments"] == {
        "all": True
    }


def test_status_requires_exactly_one_locator(checkout: Path) -> None:
    with pytest.raises(ValueError, match="provide run_id or repo_path"):
        server.quill_status()
    with pytest.raises(ValueError, match="not both"):
        server.quill_status("r1", str(checkout))


def test_mcp_exposes_exact_async_tool_surface() -> None:
    tools = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert tools == {
        "quill_start",
        "quill_status",
        "quill_run_breakdown",
        "quill_recent_runs",
        "quill_queue",
        "quill_stop",
        "quill_answer",
        "quill_restart",
    }
    assert "dry_run" not in inspect.signature(server.quill_start).parameters
