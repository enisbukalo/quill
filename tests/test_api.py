"""FastAPI quill-api tests — the multi-repo service surface."""

from __future__ import annotations

import base64
import io
import json
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from collections.abc import Sequence

from quill_api.model_registry import ServiceModelRegistry, SwitchableModel
from quill_api.model_registry import SwitchState as ModelSwitchState

from quill.bootstrap import seed_personas
from quill.git_ops import PullRequest
from quill_api.app import create_app
from quill_api.queue import QueuedRun
from quill_api.pr_watcher import ReviewCandidate
from quill_api.repository_registry import ConfiguredRepository
from quill_api.services import Services
from quill_api.settings import Settings
from quill_api.state import PhaseEntry, RunState, RunStatus
from quill_api.workspace import (
    ConfigWorkspace,
    Workspace,
    WorkspaceBranch,
    WorkspaceConflict,
    WorkspaceGitError,
    WorkspaceMutation,
    WorkspaceNotFound,
    RestartBranchStatus,
)
import quill_api.services as services_module
import quill_api.runner as runner_module

_CONFIG = """\
[repo]
name = "me/proj"
pr_base = "main"

[runner]
kind = "opencode"

[build]
command = "make"
test = "make test"

[[phase]]
id = "plan"
type = "producer"
persona = "plan.md"
model = "m"
artifact = "plan.md"
produces_contract = "quill.plan/v1"
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings entirely inside tmp_path, so nothing touches the real machine."""
    state = tmp_path / "state"
    return Settings(
        state_dir=state,
        workspace_root=state / "workspaces",
        runs_root=state / "runs",
        personas_root=tmp_path / "personas",
        skills_root=tmp_path / "skills",
        db_url="sqlite+pysqlite:///:memory:",
        vllm_url="http://vllm.example:8000",
    )


@pytest.fixture
def gate() -> threading.Event:
    """Held closed while a test runs; released before the app shuts down."""
    return threading.Event()


@pytest.fixture
def services(settings: Settings, gate: threading.Event) -> Services:
    """Services whose runs block instead of executing.

    The pipeline itself has its own tests and needs models, git, and GitHub. Holding each run open
    also makes the queue observable: with a no-op executor the worker drains a submission before
    the next request even arrives, so nothing ever appears to be waiting.
    """

    def hold(_run: QueuedRun) -> None:
        gate.wait(timeout=5)

    svc = Services(settings)
    svc.queue._execute = hold  # type: ignore[method-assign]
    return svc


@pytest.fixture
def client(services: Services, gate: threading.Event) -> Iterator[TestClient]:
    with TestClient(create_app(services)) as test_client:
        yield test_client
        # Release inside the context manager: leaving the `with` runs lifespan shutdown, which
        # joins the queue worker. A still-blocked run would make every teardown wait it out.
        gate.set()


def _start(client: TestClient, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "repo": "me/proj",
        "branch": "fix/ticket_1",
        "ticket": 1,
    }
    body.update(overrides)
    response = client.post("/runs", json=body)
    assert response.status_code == 202, response.text
    result: dict[str, object] = response.json()
    return result


def _queued(run_id: str) -> QueuedRun:
    return QueuedRun(run_id=run_id, repo="me/proj", branch="b", ticket=1, mode="create")


def test_automatic_pr_review_admission_is_idempotent(services: Services) -> None:
    candidate = ReviewCandidate("me/proj", "feature", 14, 31, "abc123")

    assert services._admit_pr_review(candidate)
    assert not services._admit_pr_review(candidate)

    pending = services.queue.pending()
    assert len(pending) == 1
    assert pending[0].workflow == "pr_review"
    assert pending[0].pr_head_sha == "abc123"


def _write_pr_review(settings: Settings, run_id: str, verdict: str) -> None:
    findings = []
    if verdict == "BLOCK":
        findings = [
            {
                "id": "PRR-001",
                "severity": "MAJOR",
                "title": "Broken path",
                "requirement": "The ticket requires this path",
                "evidence": "src/app.py:42 returns false",
                "failure_scenario": "A normal request reaches this branch",
                "impact": "The requested behavior is unavailable",
                "required_outcome": "The normal request must succeed",
            }
        ]
    path = settings.runs_root / run_id / "pr-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"verdict": verdict, "summary": "reviewed", "findings": findings}),
        encoding="utf-8",
    )


def test_pr_feedback_block_queues_one_sha_bound_update(
    services: Services, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services_module,
        "pr_target_for_repo",
        lambda *_args: PullRequest(31, "feature", "title", "url", "abc123"),
    )
    state = RunState(
        run_id="review-block",
        ticket=14,
        repo="me/proj",
        branch="feature",
        mode="review",
        workflow="pr_review",
        pr_number=31,
        pr_head_sha="abc123",
        status=RunStatus.DONE,
    )
    _write_pr_review(settings, state.run_id, "BLOCK")

    services._on_run_terminal(state)
    services._on_run_terminal(state)

    pending = services.queue.pending()
    assert len(pending) == 1
    assert pending[0].mode == "update"
    assert pending[0].workflow == "pr_update"
    assert pending[0].pr_head_sha == "abc123"
    assert pending[0].feedback_digest


def test_pr_feedback_pass_and_stale_block_do_not_queue(
    services: Services, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_head = "pass-head"
    monkeypatch.setattr(
        services_module,
        "pr_target_for_repo",
        lambda *_args: PullRequest(31, "feature", "title", "url", current_head),
    )
    passed = RunState(
        run_id="review-pass",
        ticket=14,
        repo="me/proj",
        branch="feature",
        mode="review",
        workflow="pr_review",
        pr_number=31,
        pr_head_sha="pass-head",
        status=RunStatus.DONE,
    )
    _write_pr_review(settings, passed.run_id, "PASS")
    services._on_run_terminal(passed)

    stale = RunState(
        run_id="review-stale",
        ticket=14,
        repo="me/proj",
        branch="feature",
        mode="review",
        workflow="pr_review",
        pr_number=31,
        pr_head_sha="old-head",
        status=RunStatus.DONE,
    )
    _write_pr_review(settings, stale.run_id, "BLOCK")
    services._on_run_terminal(stale)

    assert services.queue.pending() == []


# -- system -----------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "up"
    assert body["gh_available"] is True
    assert body["queue_depth"] == 0


def test_version(client: TestClient) -> None:
    body = client.get("/version").json()
    assert body["quill"] == body["api"]


def test_lifetime_stats_zero_state(client: TestClient) -> None:
    body = client.get("/stats").json()

    assert body == {
        "total_runs": 0,
        "successful_runs": 0,
        "failed_runs": 0,
        "halted_runs": 0,
        "other_runs": 0,
        "repositories": 0,
        "tickets": 0,
        "duration_s": 0.0,
        "context_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "phase_executions": 0,
        "tool_calls": 0,
        "self_checks": 0,
        "repeat_attempts": 0,
        "model_loads": 0,
        "model_load_duration_s": 0.0,
        "models": [],
        "phases": [],
        "recent_runs": [],
        "failures": [],
    }


def test_lifetime_stats_aggregate_runs_and_models(client: TestClient, services: Services) -> None:
    completed = RunState(
        run_id="completed",
        ticket=7,
        repo="me/proj",
        workflow="pr_review",
        status=RunStatus.DONE,
        started_at=1.0,
    )
    failed = RunState(
        run_id="failed",
        ticket=8,
        repo="me/proj",
        status=RunStatus.FAILED,
        started_at=1.0,
    )
    services.history.record(completed)
    services.history.record(failed)
    services.history.record_breakdown(
        completed.run_id,
        {
            "cumulative_usage": {
                "context_tokens": 100,
                "output_tokens": 25,
                "total_tokens": 125,
                "cost": 0.75,
            },
            "phase_executions": [
                {
                    "phase": "impl",
                    "label": "implement",
                    "model": "gemma-test",
                    "call_number": 2,
                    "is_retry": True,
                    "duration_s": 12.5,
                    "context_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                    "context_window_tokens": 80,
                    "cost": 0.75,
                    "tool_calls_total": 4,
                    "self_check_status": "passed",
                }
            ],
            "model_loads": [
                {
                    "status": "completed",
                    "duration_s": 31.25,
                }
            ],
        },
        1,
    )

    body = client.get("/stats").json()

    assert body["total_runs"] == 2
    assert body["successful_runs"] == 1
    assert body["failed_runs"] == 1
    assert body["repositories"] == 1
    assert body["tickets"] == 2
    assert body["context_tokens"] == 55
    assert body["output_tokens"] == 25
    assert body["total_tokens"] == 80
    assert body["cost"] == 0.75
    assert body["duration_s"] > 0
    assert body["phase_executions"] == 1
    assert body["tool_calls"] == 4
    assert body["self_checks"] == 1
    assert body["repeat_attempts"] == 1
    assert body["model_loads"] == 1
    assert body["model_load_duration_s"] == 31.25
    assert body["phases"] == [
        {
            "phase": "impl",
            "label": "implement",
            "executions": 1,
            "duration_s": 12.5,
            "total_tokens": 80,
            "tool_calls": 4,
        }
    ]
    assert [point["run_id"] for point in body["recent_runs"]] == ["completed", "failed"]
    assert body["recent_runs"][0]["total_tokens"] == 80
    assert body["recent_runs"][0]["workflow"] == "pr_review"
    assert body["models"] == [
        {
            "model": "gemma-test",
            "calls": 1,
            "duration_s": 12.5,
            "context_tokens": 55,
            "output_tokens": 25,
            "total_tokens": 80,
            "cost": 0.75,
        }
    ]


def test_openapi_served(client: TestClient) -> None:
    """The schema is how an agent discovers the API, so it must always render."""
    schema = client.get("/openapi.json").json()
    for path in (
        "/runs",
        "/runs/{run_id}/restart",
        "/runs/{run_id}/restart-options",
        "/queue",
        "/personas",
        "/skills",
        "/init",
        "/stats",
        "/settings/telemetry",
        "/events",
        "/memories",
        "/github/repositories",
        "/github/repositories/{owner}/{name}/issues",
        "/workspaces",
        "/workspaces/{owner}/{name}/branches",
    ):
        assert path in schema["paths"], path
    assert "dry_run" not in schema["components"]["schemas"]["StartRunRequest"]["properties"]
    assert "config" not in schema["components"]["schemas"]["StartRunRequest"]["properties"]


def test_failed_run_can_restart_from_recorded_phase(
    client: TestClient,
    services: Services,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = RunState(
        run_id="failed-source",
        ticket=17,
        repo="me/proj",
        branch="feature_17",
        status=RunStatus.FAILED,
        started_at=1.0,
        history=[
            PhaseEntry(
                phase="plan",
                label="Write plan",
                verdict="DONE",
                attempt=1,
                ts=2.0,
                phase_type="producer",
                model="model-35b",
                duration_s=1.0,
            ),
            PhaseEntry(
                phase="impl",
                label="Implement",
                verdict="CRASH",
                attempt=1,
                ts=4.0,
                phase_type="producer",
                model="model-35b",
                duration_s=2.0,
            ),
            PhaseEntry(
                phase="review.architecture",
                label="Architecture audit",
                verdict="DONE",
                attempt=1,
                ts=6.0,
                phase_type="reviewer",
                model="model-35b",
                duration_s=1.0,
            ),
        ],
    )
    services.store.add(source)
    services.history.record(source)
    run_dir = settings.runs_root / source.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "phase-checkpoints.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": source.run_id,
                "repo": source.repo,
                "branch": source.branch,
                "base": "base-sha",
                "phases": ["plan", "impl", "review", "test"],
                "checkpoints": [
                    {"phase": "plan", "commit": "plan-sha"},
                    {"phase": "impl", "commit": "impl-sha"},
                    {"phase": "review", "commit": "review-sha"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "state.jsonl").write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "run_queued",
                        "ts": 1.0,
                        "model_overrides": {"plan": "model-35b", "impl": "model-35b"},
                    }
                ),
                json.dumps(
                    {
                        "type": "run_plan",
                        "ts": 1.0,
                        "phase_set_hash": "test-phase-set",
                        "phase_graph": {
                            "nodes": [
                                {
                                    "id": "plan",
                                    "label": "Plan",
                                    "type": "producer",
                                    "order": 0,
                                },
                                {
                                    "id": "impl",
                                    "label": "Implement",
                                    "type": "producer",
                                    "order": 1,
                                },
                                {
                                    "id": "review.architecture",
                                    "label": "Architecture audit",
                                    "type": "reviewer",
                                    "order": 2,
                                },
                            ],
                            "edges": [],
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        services.workspaces,
        "restart_status",
        lambda *_args, **_kwargs: RestartBranchStatus(True, ahead=2),
    )

    options = client.get(f"/runs/{source.run_id}/restart-options")
    assert options.status_code == 200
    assert [phase["id"] for phase in options.json()["phases"]] == [
        "plan",
        "impl",
        "review.architecture",
    ]
    assert options.json()["phases"][2]["start_phase"] == "review"

    impl_choice = options.json()["phases"][1]
    response = client.post(
        f"/runs/{source.run_id}/restart",
        json={"phase": "impl", "sequence": impl_choice["sequence"]},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["source_run_id"] == source.run_id
    assert body["start_phase"] == "impl"
    restarted = services.store.get(body["run_id"])
    assert restarted is not None
    assert restarted.branch == "feature_17"
    assert [entry.phase for entry in restarted.history] == ["plan"]
    queued = services.queue.pending()[0]
    assert dict(queued.model_overrides) == {
        "impl": "model-35b",
        "plan": "model-35b",
        "review": "model-35b",
    }


def test_dashboard_and_assets_are_served(client: TestClient) -> None:
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.headers["content-type"].startswith("text/html")
    assert "Quill Control Center" in dashboard.text
    assert 'src="/assets/app.mjs"' in dashboard.text
    assert 'href="/assets/favicon.svg"' in dashboard.text
    assert 'data-route="workspaces"' in dashboard.text
    assert 'data-route="memories"' in dashboard.text
    assert 'data-route="settings"' in dashboard.text

    stylesheet = client.get("/assets/styles.css")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    favicon = client.get("/assets/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")


def test_telemetry_display_settings_are_validated_and_persisted(client: TestClient) -> None:
    defaults = client.get("/settings/telemetry")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "cpu_temperature_min_c": 20.0,
        "cpu_temperature_max_c": 70.0,
        "gpu_temperature_min_c": 20.0,
        "gpu_temperature_max_c": 80.0,
    }

    changed = {
        "cpu_temperature_min_c": 25,
        "cpu_temperature_max_c": 75,
        "gpu_temperature_min_c": 30,
        "gpu_temperature_max_c": 85,
    }
    assert client.put("/settings/telemetry", json=changed).json() == changed
    assert client.get("/settings/telemetry").json() == changed

    invalid = client.put(
        "/settings/telemetry",
        json={**changed, "cpu_temperature_min_c": 90},
    )
    assert invalid.status_code == 422
    assert client.get("/assets/not-a-real-file.css").status_code == 404


def test_dashboard_can_be_served_from_an_external_release(
    settings: Settings, tmp_path: Path
) -> None:
    web_root = tmp_path / "web" / "current"
    web_root.mkdir(parents=True)
    (web_root / "index.html").write_text("<h1>external dashboard</h1>", encoding="utf-8")
    (web_root / "app.mjs").write_text("export const external = true;", encoding="utf-8")
    services = Services(replace(settings, web_root=web_root))

    with TestClient(create_app(services)) as external_client:
        assert external_client.get("/").text == "<h1>external dashboard</h1>"
        assert "external = true" in external_client.get("/assets/app.mjs").text


def test_models_endpoint_answers_even_with_no_gpu_stack(client: TestClient) -> None:
    """The service must stay up to *say* the model server is down — being able to ask that from
    another machine is half the point of running it as a service."""
    body = client.get("/models").json()
    assert body["backend"] == "vllm"
    assert isinstance(body["reachable"], bool)


def test_models_endpoint_reports_vllm_model_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quill_api.routers import system as system_router

    class FakeVllmServer:
        def __init__(self, _url: str) -> None:
            pass

        def __enter__(self) -> FakeVllmServer:
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def healthy(self) -> bool:
            return True

        def model_cards(self) -> list[dict[str, object]]:
            return [
                {
                    "id": "Qwen3.6_27B_NVFP4",
                    "max_model_len": 200_000,
                    "root": "/models/qwen",
                }
            ]

    monkeypatch.setattr(system_router, "VllmServer", FakeVllmServer)

    body = client.get("/models").json()

    assert body["backend"] == "vllm"
    assert body["reachable"] is True
    assert body["loaded"] == ["Qwen3.6_27B_NVFP4"]
    assert body["model_details"] == [
        {
            "id": "Qwen3.6_27B_NVFP4",
            "max_model_len": 200_000,
            "root": "/models/qwen",
        }
    ]


def test_github_choices_use_authenticated_repositories_and_observed_conventions(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quill_api.routers import github as github_router

    def fake_gh(*args: str) -> object:
        if args[:2] == ("api", "user"):
            return {"login": "me"}
        if args[:2] == ("repo", "list"):
            return [
                {
                    "nameWithOwner": "me/older",
                    "visibility": "PUBLIC",
                    "updatedAt": "2026-01-01T00:00:00Z",
                },
                {
                    "nameWithOwner": "me/current",
                    "visibility": "PRIVATE",
                    "updatedAt": "2026-07-27T00:00:00Z",
                },
            ]
        if args[:2] == ("issue", "list"):
            return [
                {
                    "number": 127,
                    "title": "Implement Vllm Capabilities",
                    "labels": [{"name": "documentation"}, {"name": "enhancement"}],
                },
                {"number": 126, "title": "Fix Source Of Truth", "labels": [{"name": "bug"}]},
            ]
        if args[:2] == ("label", "list"):
            return [
                {"name": "enhancement"},
                {"name": "EPIC"},
                {"name": "documentation"},
                {"name": "bug"},
            ]
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(github_router, "_gh", fake_gh)
    monkeypatch.setattr(github_router, "_ensure_gh", lambda: None)
    services.repositories._repositories = (  # noqa: SLF001 - seed cached registry fixture
        ConfiguredRepository("me/current", "PRIVATE", "2026-07-27T00:00:00Z", "main", "a"),
        ConfiguredRepository("me/older", "PUBLIC", "2026-01-01T00:00:00Z", "main", "b"),
    )

    repositories = client.get("/github/repositories").json()
    assert repositories["login"] == "me"
    assert [item["name"] for item in repositories["repositories"]] == [
        "me/current",
        "me/older",
    ]
    issues = client.get("/github/repositories/me/current/issues").json()
    assert [issue["number"] for issue in issues["issues"]] == [126, 127]
    assert issues["work_types"] == ["bug", "documentation", "enhancement", "epic"]


def test_workflow_choices_expose_parallel_producer_groups(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quill_api.routers import github as github_router

    config = """
[repo]
name = "me/proj"
[runner]
kind = "pi"
backend = "vllm"
[build]
command = "make"
test = "make test"
[workflows]
default = "ticket"
[workflows.ticket]
label = "New ticket"
mode = "create"

[[workflows.ticket.phase]]
id = "requirements"
type = "producer"
persona = "plan.md"
model = "qwen"
artifact = "requirements.md"
parallel_group = "research"
produces_contract = "quill.research.requirements/v1"

[[workflows.ticket.phase]]
id = "technical"
type = "producer"
persona = "plan.md"
model = "qwen"
artifact = "technical.md"
parallel_group = "research"
produces_contract = "quill.research.technical/v1"
"""
    encoded = base64.b64encode(config.encode()).decode()
    services.settings.personas_root.mkdir(parents=True, exist_ok=True)
    (services.settings.personas_root / "plan.md").write_text("persona", encoding="utf-8")
    monkeypatch.setattr(github_router, "_ensure_gh", lambda: None)
    monkeypatch.setattr(github_router, "_gh", lambda *_args: {"content": encoded})
    services.repositories._repositories = (  # noqa: SLF001 - seed cached registry fixture
        ConfiguredRepository("me/proj", "PRIVATE", "2026-08-02T00:00:00Z", "main", "abc"),
    )

    response = client.get("/github/repositories/me/proj/workflows")

    assert response.status_code == 200, response.text
    phases = response.json()["workflows"][0]["phases"]
    assert [(phase["id"], phase["parallel_group"]) for phase in phases] == [
        ("requirements", "research"),
        ("technical", "research"),
    ]


# -- starting runs ----------------------------------------------------------------


def test_start_run_is_accepted_and_queued(client: TestClient, settings: Settings) -> None:
    body = _start(client)
    assert body["status"] == "queued"
    assert body["repo"] == "me/proj"
    assert body["branch"] == "fix/ticket_1"
    assert body["queue_position"] == 0
    assert body["activity"] == "queued"
    assert body["activity_label"] == "Queued"
    state_lines = (
        (settings.runs_root / str(body["run_id"]) / "state.jsonl").read_text().splitlines()
    )
    queued = json.loads(state_lines[0])
    assert queued["type"] == "run_queued"
    assert queued["repo"] == "me/proj"
    assert queued["ticket"] == 1


def test_every_second_active_submission_is_rejected(client: TestClient) -> None:
    _start(client)
    assert (
        client.post(
            "/runs", json={"repo": "you/other", "branch": "feat/ticket_2", "ticket": 2}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/runs", json={"repo": "me/proj", "branch": "fix/ticket_1", "ticket": 1}
        ).status_code
        == 409
    )
    assert client.get("/queue").json()["depth"] == 1


def test_allow_duplicate_is_not_an_api_field(client: TestClient) -> None:
    response = client.post(
        "/runs",
        json={"repo": "me/proj", "branch": "ticket-1", "ticket": 1, "allow_duplicate": True},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticket", 0),
        ("ticket", -3),
        ("ticket", "not-an-int"),
        ("repo", "no-slash"),
        ("repo", "../../etc/passwd"),
        ("branch", ""),
        ("mode", "sideways"),
    ],
)
def test_start_run_rejects_bad_input(client: TestClient, field: str, value: object) -> None:
    body: dict[str, object] = {
        "repo": "me/proj",
        "branch": "b",
        "ticket": 1,
    }
    body[field] = value
    assert client.post("/runs", json=body).status_code == 422


def test_start_run_requires_gh(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from quill_api.routers import runs as runs_router

    monkeypatch.setattr(runs_router, "gh_authenticated", lambda: False)
    response = client.post("/runs", json={"repo": "me/proj", "branch": "b", "ticket": 1})
    assert response.status_code == 424


def test_start_run_rejects_removed_dry_run_field(client: TestClient) -> None:
    response = client.post(
        "/runs",
        json={
            "repo": "me/proj",
            "branch": "b",
            "ticket": 1,
            "dry_run": True,
        },
    )
    assert response.status_code == 422


def test_start_run_rejects_removed_config_field(client: TestClient) -> None:
    response = client.post(
        "/runs",
        json={"repo": "me/proj", "branch": "b", "ticket": 1, "config": _CONFIG},
    )
    assert response.status_code == 422


def test_start_run_captures_sorted_model_overrides(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[QueuedRun] = []
    monkeypatch.setattr(services.queue, "submit", lambda run: captured.append(run) or 0)

    response = client.post(
        "/runs",
        json={
            "repo": "me/proj",
            "branch": "ticket-1",
            "ticket": 1,
            "model_overrides": {"review": "qwen", "plan": "gemma"},
        },
    )

    assert response.status_code == 202
    assert captured[0].model_overrides == (("plan", "gemma"), ("review", "qwen"))
    queued_event = json.loads(
        (services.settings.runs_root / response.json()["run_id"] / "state.jsonl").read_text()
    )
    assert queued_event["model_overrides"] == {"plan": "gemma", "review": "qwen"}


def test_run_fails_clearly_when_checked_out_repo_has_no_config(
    services: Services, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace("me/proj", "main", settings.workspace_root / "me" / "proj")
    workspace.path.mkdir(parents=True)
    monkeypatch.setattr(
        services.workspaces,
        "prepare_for_config",
        lambda _repo, _branch: ConfigWorkspace(workspace, requested_branch_exists=False),
    )
    monkeypatch.setattr(services.bus, "publish_threadsafe", lambda _event: None)
    run = RunState(run_id="missing-config", repo="me/proj", branch="main", ticket=1)
    services.store.add(run)

    services.manager.execute(_queued(run.run_id))

    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert "no quillfolio.toml" in run.error


def test_automatic_update_rejects_feedback_for_an_old_pr_head(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaleGit:
        def pr_target_for_ticket(self, _ticket: int) -> PullRequest:
            return PullRequest(31, "feature", "title", "url", "new-head")

    monkeypatch.setattr(runner_module, "GitOps", lambda _runner: StaleGit())
    monkeypatch.setattr(services.workspaces, "local_branches", lambda _repo: {"feature"})
    monkeypatch.setattr(services.manager, "_live_publish", lambda _event, _state: None)
    run = RunState(
        run_id="stale-update",
        repo="me/proj",
        branch="feature",
        ticket=14,
        mode="update",
        workflow="pr_update",
    )
    services.store.add(run)

    services.manager.execute(
        QueuedRun(
            run_id=run.run_id,
            repo=run.repo,
            branch="feature",
            ticket=14,
            mode="update",
            workflow="pr_update",
            pr_number=31,
            pr_head_sha="old-head",
        )
    )

    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert "feedback is stale" in run.error


def test_new_branch_loads_default_branch_config_then_uses_configured_base(
    services: Services, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Workspace("me/proj", "trunk", settings.workspace_root / "me" / "proj")
    source.path.mkdir(parents=True)
    (source.path / "quillfolio.toml").write_text(
        _CONFIG.replace('pr_base = "main"', 'pr_base = "release"'), encoding="utf-8"
    )
    seed_personas(settings.personas_root)
    monkeypatch.setattr(
        services.workspaces,
        "prepare_for_config",
        lambda _repo, _branch: ConfigWorkspace(source, requested_branch_exists=False),
    )
    prepared: dict[str, object] = {}

    def prepare(repo: str, branch: str, *, base: str) -> Workspace:
        prepared.update(repo=repo, branch=branch, base=base)
        return Workspace(repo, branch, source.path)

    monkeypatch.setattr(services.workspaces, "prepare", prepare)
    monkeypatch.setattr(services.manager, "_deps_for", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        services.manager,
        "_drive",
        lambda state, queued, config, deps, directory, run_dir: prepared.update(
            driven_directory=directory
        ),
    )
    run = RunState(run_id="new-branch", repo="me/proj", branch="ticket-1", ticket=1)
    services.store.add(run)

    services.manager.execute(_queued(run.run_id))

    assert prepared["base"] == "release"
    assert prepared["branch"] == "b"
    assert prepared["driven_directory"] == source.path


# -- queue + run views ------------------------------------------------------------


def test_queue_reports_what_is_waiting(client: TestClient) -> None:
    _start(client)

    body = client.get("/queue").json()

    assert body["depth"] == 1
    assert [r["ticket"] for r in body["queued"]] == [1]
    assert body["active"] is None


def test_run_detail_and_missing_run(client: TestClient) -> None:
    started = _start(client)

    detail = client.get(f"/runs/{started['run_id']}").json()

    assert detail["ticket"] == 1
    assert detail["history"] == []
    assert client.get("/runs/nope").status_code == 404


def test_runs_list_filters_by_repo(client: TestClient, services: Services) -> None:
    _start(client)
    services.store.add(RunState(run_id="other", ticket=9, repo="you/other"))

    listed = client.get("/runs", params={"repo": "me/proj"}).json()["runs"]

    assert {r["repo"] for r in listed} == {"me/proj"}


def test_runs_list_filters_by_ticket_and_status(client: TestClient, services: Services) -> None:
    _start(client, ticket=1)
    services.store.add(RunState(run_id="done-2", ticket=2, repo="me/proj", status=RunStatus.DONE))

    listed = client.get("/runs", params={"ticket": 2, "status": "done"}).json()["runs"]

    assert [run["run_id"] for run in listed] == ["done-2"]


def test_runs_list_paginates_with_a_fixed_maximum_page(
    client: TestClient, services: Services
) -> None:
    for ticket in range(1, 203):
        services.history.record(
            RunState(
                run_id=f"history-{ticket:03d}",
                ticket=ticket,
                repo="me/proj",
                status=RunStatus.DONE,
            )
        )

    first = client.get("/runs").json()
    second = client.get("/runs", params={"offset": 200}).json()

    assert len(first["runs"]) == 200
    assert first["limit"] == 200
    assert first["offset"] == 0
    assert first["has_more"] is True
    assert len(second["runs"]) == 2
    assert second["offset"] == 200
    assert second["has_more"] is False


def test_runs_list_projects_last_phase_from_durable_history(
    client: TestClient, services: Services, settings: Settings
) -> None:
    run = RunState(run_id="finished", ticket=2, repo="me/proj", status=RunStatus.DONE)
    services.history.record(run)
    run_dir = settings.runs_root / run.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.jsonl").write_text(
        json.dumps(
            {
                "type": "phase_started",
                "phase": "build",
                "label": "Build executables",
                "phase_type": "mechanical",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    listed = client.get("/runs", params={"ticket": 2}).json()["runs"]

    assert listed[0]["phase"] == "build"
    assert listed[0]["phase_label"] == "Build executables"


def test_bulk_delete_removes_terminal_runs_and_artifacts(
    client: TestClient, services: Services, settings: Settings
) -> None:
    run = RunState(run_id="finished", ticket=2, repo="me/proj", status=RunStatus.DONE)
    services.store.add(run)
    services.history.record(run)
    services.history.record_breakdown(run.run_id, {"schema_version": 1}, 1)
    run_dir = settings.runs_root / run.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "plan.md").write_text("done", encoding="utf-8")

    response = client.request("DELETE", "/runs", json={"run_ids": [run.run_id]})

    assert response.status_code == 200
    assert response.json() == {"deleted": [run.run_id]}
    assert services.store.get(run.run_id) is None
    assert services.history.get(run.run_id) is None
    assert services.history.get_breakdown(run.run_id) is None
    assert not run_dir.exists()
    stats = client.get("/stats").json()
    assert stats["total_runs"] == 1
    assert stats["successful_runs"] == 1


def test_bulk_delete_rejects_active_or_unknown_runs(client: TestClient) -> None:
    started = _start(client)

    active = client.request("DELETE", "/runs", json={"run_ids": [started["run_id"]]})
    missing = client.request("DELETE", "/runs", json={"run_ids": ["missing"]})

    assert active.status_code == 409
    assert missing.status_code == 404


def test_memories_list_and_delete_selected_or_all(client: TestClient, settings: Settings) -> None:
    memory_file = settings.memory_root / "me" / "proj" / "blockers.jsonl"
    memory_file.parent.mkdir(parents=True)
    events = [
        {
            "event": "blocked",
            "blocker_id": "one",
            "fingerprint": "fingerprint-one",
            "repo": "me/proj",
            "phase": "review_plan",
            "finding": "Use the real lifecycle callback",
            "at": "2026-07-30T01:00:00+00:00",
        },
        {
            "event": "resolved",
            "blocker_id": "one",
            "fingerprint": "fingerprint-one",
            "repo": "me/proj",
            "phase": "review_plan",
            "finding": "Use the real lifecycle callback",
            "changed_files": ["src/main.gd"],
            "verified_by": "review_plan:PASS",
            "at": "2026-07-30T01:01:00+00:00",
        },
        {
            "event": "blocked",
            "blocker_id": "unresolved",
            "fingerprint": "fingerprint-two",
            "repo": "me/proj",
            "phase": "review_impl_final",
            "finding": "Unresolved finding",
            "at": "2026-07-30T01:02:00+00:00",
        },
    ]
    memory_file.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    listing = client.get("/memories")

    assert listing.status_code == 200
    body = listing.json()
    assert body["archived_events"] == 3
    assert len(body["memories"]) == 1
    assert body["memories"][0]["finding"] == "Use the real lifecycle callback"
    assert body["memories"][0]["changed_files"] == ["src/main.gd"]
    memory_id = body["memories"][0]["memory_id"]

    selected = client.request(
        "DELETE", "/memories", json={"memory_ids": [memory_id], "delete_all": False}
    )
    assert selected.status_code == 200
    assert selected.json() == {"deleted": [memory_id]}
    assert client.get("/memories").json() == {"memories": [], "archived_events": 1}

    cleared = client.request("DELETE", "/memories", json={"delete_all": True})
    assert cleared.status_code == 200
    assert cleared.json() == {"deleted": []}
    assert client.get("/memories").json() == {"memories": [], "archived_events": 0}


def test_memories_delete_requires_selection_or_all(client: TestClient) -> None:
    response = client.request("DELETE", "/memories", json={})

    assert response.status_code == 422


def test_stop_unknown_run_404(client: TestClient) -> None:
    assert client.post("/runs/nope/stop").status_code == 404


def test_stop_accepts_a_queued_run(client: TestClient) -> None:
    started = _start(client)
    response = client.post(f"/runs/{started['run_id']}/stop")
    assert response.status_code == 200
    assert response.json()["status"] in {"queued", "halted"}


def test_decision_without_a_pending_question_409(client: TestClient) -> None:
    started = _start(client)
    response = client.post(f"/runs/{started['run_id']}/decision", json={"answer": "yes"})
    assert response.status_code == 409


def test_decision_requires_an_answer(client: TestClient) -> None:
    started = _start(client)
    assert client.post(f"/runs/{started['run_id']}/decision", json={}).status_code == 422


# -- artifacts --------------------------------------------------------------------


def test_artifacts_list_and_read(client: TestClient, settings: Settings) -> None:
    run_dir = settings.runs_root / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.md").write_text("the plan", encoding="utf-8")

    listed = client.get("/runs/run-1/artifacts").json()
    assert [a["name"] for a in listed["artifacts"]] == ["plan.md"]

    content = client.get("/runs/run-1/artifacts/plan.md").json()
    assert content["content"] == "the plan"

    downloaded = client.get("/runs/run-1/artifact-downloads/plan.md")
    assert downloaded.content == b"the plan"
    assert "attachment" in downloaded.headers["content-disposition"]

    archive = client.get("/runs/run-1/artifacts.zip")
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert bundle.namelist() == ["plan.md"]
        assert bundle.read("plan.md") == b"the plan"


def test_artifacts_of_an_unknown_run_are_empty(client: TestClient) -> None:
    assert client.get("/runs/nope/artifacts").json()["artifacts"] == []


def test_breakdown_is_persisted_and_returned_complete(
    client: TestClient, services: Services, settings: Settings
) -> None:
    run_id = "breakdown-1"
    state = RunState(run_id=run_id, ticket=7, repo="me/proj", status=RunStatus.DONE)
    services.store.add(state)
    run_dir = settings.runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.jsonl").write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {
                    "type": "phase_started",
                    "ts": 1.0,
                    "phase": "impl",
                    "label": "implement",
                    "attempt": 1,
                    "phase_type": "producer",
                    "model": "m",
                },
                {
                    "type": "phase_done",
                    "ts": 2.0,
                    "phase": "impl",
                    "label": "implement",
                    "verdict": "DONE",
                    "duration_s": 1.0,
                    "tools": {"read": 1},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "stream-impl-m-1.jsonl").write_text(
        '{"type":"tool_execution_start","toolCallId":"x","toolName":"read","args":{"path":"secret"}}\n'
        '{"type":"tool_execution_end","toolCallId":"x","toolName":"read","result":{"value":"full"},"isError":false}\n',
        encoding="utf-8",
    )

    body = client.get(f"/runs/{run_id}/breakdown").json()

    assert body["phase_executions"][0]["tool_calls_total"] == 1
    assert body["phase_executions"][0]["tool_calls_by_name"] == {"read": 1}
    assert "tool_calls" not in body["phase_executions"][0]
    assert services.history.get_breakdown(run_id) is not None
    assert client.get("/runs/missing/breakdown").status_code == 404


def test_artifact_traversal_is_blocked(client: TestClient, settings: Settings) -> None:
    """The name comes straight from the URL, so it must not reach outside the run dir."""
    (settings.runs_root / "run-1").mkdir(parents=True)
    (settings.state_dir / "secret.txt").write_text("classified", encoding="utf-8")

    response = client.get("/runs/run-1/artifacts/../secret.txt")

    assert response.status_code in (400, 404)
    assert "classified" not in response.text


# -- events -----------------------------------------------------------------------


def test_events_route_is_mounted(client: TestClient) -> None:
    """Presence only. The SSE generator is open-ended by design — it runs until the client
    disconnects — so consuming it through the *sync* TestClient would block the test forever. Live
    fan-out is covered by the EventBus unit tests in test_events.py.
    """
    schema = client.get("/openapi.json").json()
    assert "get" in schema["paths"]["/events"]


# -- init -------------------------------------------------------------------------


def test_init_describes_the_schema_and_catalogs(client: TestClient, settings: Settings) -> None:
    seed_personas(settings.personas_root)
    skill = settings.skills_root / "cpp-pro"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: cpp-pro\ndescription: modern C++\n---\nbody", encoding="utf-8"
    )

    body = client.get("/init").json()

    assert body["config_filename"] == "quillfolio.toml"
    assert "producer" in body["config_schema"]["phase_types"]
    assert "ci_check" in body["config_schema"]["mechanical_steps"]
    assert any(p["name"] == "plan" for p in body["personas"])
    assert [s["name"] for s in body["skills"]] == ["cpp-pro"]
    assert "[[workflows.ticket.phase]]" in body["starter_config"]
    assert "[[workflows.pr_update.phase]]" in body["starter_config"]
    assert "[workflows.pr_review]" in body["starter_config"]
    assert 'mode = "review"' in body["starter_config"]


def test_init_schema_cannot_drift_from_the_loader(client: TestClient) -> None:
    """Generated from the live constants, so a new phase type or step appears here for free."""
    from quill.config import MECHANICAL_STEPS, PHASE_TYPES

    schema = client.get("/init").json()["config_schema"]

    assert schema["phase_types"] == list(PHASE_TYPES)
    assert schema["mechanical_steps"] == list(MECHANICAL_STEPS)


# -- queue execution --------------------------------------------------------------


def test_queue_executes_submissions_in_order(settings: Settings) -> None:
    """Serialised execution is the GPU invariant; FIFO is the fairness one."""
    executed: list[str] = []

    def record(run: QueuedRun) -> None:
        executed.append(run.run_id)

    services = Services(settings)
    services.queue._execute = record  # type: ignore[method-assign]
    services.queue.start()
    try:
        for index in range(3):
            services.queue.submit(_queued(f"r{index}"))
        services.queue._queue.join()
    finally:
        services.queue.stop()

    assert executed == ["r0", "r1", "r2"]


def test_a_failing_run_does_not_kill_the_queue(settings: Settings) -> None:
    """Every later run depends on that worker thread surviving."""
    executed: list[str] = []

    def explode_on_first(run: QueuedRun) -> None:
        if run.run_id == "r0":
            raise RuntimeError("boom")
        executed.append(run.run_id)

    services = Services(settings)
    services.queue._execute = explode_on_first  # type: ignore[method-assign]
    services.queue.start()
    try:
        services.queue.submit(_queued("r0"))
        services.queue.submit(_queued("r1"))
        services.queue._queue.join()
    finally:
        services.queue.stop()

    assert executed == ["r1"]


def test_cancelled_queue_item_is_never_executed(settings: Settings) -> None:
    executed: list[str] = []
    services = Services(settings)
    services.queue._execute = lambda run: executed.append(run.run_id)  # type: ignore[method-assign]
    services.queue.submit(_queued("cancelled"))

    assert services.queue.cancel("cancelled") is True
    assert services.queue.depth == 0
    services.queue.start()
    try:
        services.queue._queue.join()
    finally:
        services.queue.stop()

    assert executed == []


def test_queue_does_not_cancel_item_already_executing(settings: Settings) -> None:
    entered = threading.Event()
    release = threading.Event()

    def hold(_run: QueuedRun) -> None:
        entered.set()
        release.wait(timeout=2)

    services = Services(settings)
    services.queue._execute = hold  # type: ignore[method-assign]
    services.queue.start()
    try:
        services.queue.submit(_queued("active"))
        assert entered.wait(timeout=1)
        assert services.queue.cancel("active") is False
    finally:
        release.set()
        services.queue._queue.join()
        services.queue.stop()


def test_queue_position_clears_once_a_run_is_taken(settings: Settings) -> None:
    def noop(_run: QueuedRun) -> None:
        return None

    services = Services(settings)
    services.queue._execute = noop  # type: ignore[method-assign]
    services.queue.start()
    try:
        services.queue.submit(_queued("r0"))
        services.queue._queue.join()
    finally:
        services.queue.stop()

    assert services.queue.position("r0") is None
    assert services.queue.depth == 0


# -- history ----------------------------------------------------------------------


def test_startup_closes_out_runs_stranded_by_a_restart(settings: Settings) -> None:
    """A run whose thread died with the process would otherwise stay `running` forever."""
    services = Services(settings)
    services.history.record(
        RunState(run_id="orphan", ticket=1, repo="me/proj", status=RunStatus.RUNNING)
    )

    changed = services.history.reconcile_orphans()

    assert changed == ["orphan"]
    row = services.history.get("orphan")
    assert row is not None
    assert row.status == RunStatus.FAILED.value
    assert row.error is not None and "restarted" in row.error


def test_history_survives_a_new_process_against_the_same_file(tmp_path: Path) -> None:
    """The whole point of the file-backed DB: an always-on service gets restarted."""
    settings = Settings(
        state_dir=tmp_path,
        workspace_root=tmp_path / "w",
        runs_root=tmp_path / "r",
        personas_root=tmp_path / "p",
        skills_root=tmp_path / "s",
        db_url=f"sqlite+pysqlite:///{tmp_path / 'quill.db'}",
        vllm_url="http://vllm.example:8000",
    )
    first = Services(settings)
    first.history.record(RunState(run_id="kept", ticket=7, repo="me/proj"))

    second = Services(settings)

    assert second.history.get("kept") is not None


def test_importing_the_app_module_touches_no_filesystem(tmp_path: Path) -> None:
    """Constructing Services opens a database and creates the state directories, so a
    module-level `app` would do that on mere import — leaving a ~/.quill on any machine that so
    much as ran the test suite. The module exposes a factory instead.
    """
    import quill_api.app as app_module

    assert not hasattr(app_module, "app")
    assert callable(app_module.create_app)


# -- workspaces -------------------------------------------------------------------


def test_workspace_operations_are_in_the_openapi_schema(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    for path in (
        "/workspaces",
        "/workspaces/{owner}/{name}/branches",
        "/workspaces/{owner}/{name}/branches/{branch}/pull",
        "/workspaces/{owner}/{name}/branches/{branch}",
    ):
        assert path in schema["paths"], path
    # The branch path parameter must accept encoded '/' — it is declared as a path-type param.
    pull = schema["paths"]["/workspaces/{owner}/{name}/branches/{branch}/pull"]["post"]
    branch_param = next(p for p in pull["parameters"] if p["name"] == "branch")
    assert branch_param["in"] == "path"


def test_workspaces_list_is_empty_without_checkouts(client: TestClient) -> None:
    assert client.get("/workspaces").json() == {"workspaces": []}


def test_workspaces_list_reports_checkouts_without_leaking_paths(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services.workspaces,
        "checkouts",
        lambda: [
            Workspace(repo="me/proj", branch="main", path=Path("/server/secret/me/proj")),
            Workspace(repo="you/other", branch="dev", path=Path("/server/secret/you/other")),
        ],
    )
    body = client.get("/workspaces").json()
    assert [w["repo"] for w in body["workspaces"]] == ["me/proj", "you/other"]
    assert body["workspaces"][0]["branch"] == "main"
    # The on-disk path is server-internal and must never appear in a response.
    assert "secret" not in json.dumps(body)
    assert all("path" not in workspace for workspace in body["workspaces"])


def test_workspace_branches_are_listed_with_current_local_remote_flags(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services.workspaces,
        "branches",
        lambda repo: [
            WorkspaceBranch(name="main", current=True, local=True, remote=True),
            WorkspaceBranch(name="feature/x", current=False, local=False, remote=True),
        ],
    )
    body = client.get("/workspaces/me/proj/branches").json()
    assert body["repo"] == "me/proj"
    assert body["current"] == "main"
    assert body["branches"][1]["name"] == "feature/x"
    assert body["branches"][1]["remote"] is True
    assert body["branches"][1]["local"] is False


def test_pull_routes_a_slash_bearing_branch_and_delegates(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def fake_pull(repo: str, branch: str) -> WorkspaceMutation:
        seen["repo"] = repo
        seen["branch"] = branch
        return WorkspaceMutation(repo=repo, branch=branch, message="Fast-forwarded 'feat/x'.")

    monkeypatch.setattr(services.workspaces, "pull_branch", fake_pull)
    body = client.post("/workspaces/me/proj/branches/feat/x/pull").json()
    assert seen == {"repo": "me/proj", "branch": "feat/x"}
    assert body["branch"] == "feat/x"
    assert "Fast-forwarded" in body["message"]


def test_delete_routes_a_slash_bearing_branch_and_delegates(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def fake_delete(repo: str, branch: str) -> WorkspaceMutation:
        seen["repo"] = repo
        seen["branch"] = branch
        return WorkspaceMutation(repo=repo, branch="main", message="Deleted local 'feat/x'.")

    monkeypatch.setattr(services.workspaces, "delete_branch", fake_delete)
    body = client.request("DELETE", "/workspaces/me/proj/branches/feat/x").json()
    assert seen == {"repo": "me/proj", "branch": "feat/x"}
    assert body["branch"] == "main"


def test_workspace_branches_reject_an_invalid_repo(client: TestClient) -> None:
    assert client.get("/workspaces/own$er/proj/branches").status_code == 422


def test_missing_workspace_branches_are_404(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(repo: str) -> list[WorkspaceBranch]:
        raise WorkspaceNotFound(f"{repo} has no checkout on this server yet.")

    monkeypatch.setattr(services.workspaces, "branches", boom)
    response = client.get("/workspaces/me/proj/branches")
    assert response.status_code == 404
    assert "checkout" in response.json()["detail"]


def test_pull_maps_a_git_failure_to_502(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(repo: str, branch: str) -> WorkspaceMutation:
        raise WorkspaceGitError("could not fetch origin for me/proj.")

    monkeypatch.setattr(services.workspaces, "pull_branch", boom)
    assert client.post("/workspaces/me/proj/branches/main/pull").status_code == 502


def test_delete_maps_a_conflict_to_409(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(repo: str, branch: str) -> WorkspaceMutation:
        raise WorkspaceConflict("'main' is me/proj's default branch and cannot be deleted.")

    monkeypatch.setattr(services.workspaces, "delete_branch", boom)
    assert client.request("DELETE", "/workspaces/me/proj/branches/main").status_code == 409


def test_pull_is_blocked_while_a_queued_run_targets_the_repo(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_pull(repo: str, branch: str) -> WorkspaceMutation:
        nonlocal called
        called = True
        return WorkspaceMutation(repo=repo, branch=branch, message="ok")

    monkeypatch.setattr(services.workspaces, "pull_branch", fake_pull)
    _start(client)  # queues (and holds) a run for me/proj
    response = client.post("/workspaces/me/proj/branches/main/pull")
    assert response.status_code == 409
    assert called is False  # the manager was never asked to mutate


def test_delete_is_blocked_while_a_running_run_targets_the_repo(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services.workspaces,
        "delete_branch",
        lambda repo, branch: WorkspaceMutation(repo=repo, branch=branch, message="ok"),
    )
    services.store.add(
        RunState(run_id="r1", ticket=1, repo="me/proj", branch="b", status=RunStatus.RUNNING)
    )
    assert client.request("DELETE", "/workspaces/me/proj/branches/feature").status_code == 409


def test_workspace_reads_and_other_repos_are_unaffected_by_an_active_run(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services.workspaces,
        "branches",
        lambda repo: [WorkspaceBranch(name="main", current=True, local=True, remote=True)],
    )
    monkeypatch.setattr(
        services.workspaces,
        "pull_branch",
        lambda repo, branch: WorkspaceMutation(repo=repo, branch=branch, message="ok"),
    )
    _start(client)  # active run on me/proj
    # Reads stay allowed even for the busy repo.
    assert client.get("/workspaces/me/proj/branches").status_code == 200
    # A mutation on an unrelated, idle repo is not blocked.
    assert client.post("/workspaces/you/other/branches/main/pull").status_code == 200


# -- model switching ------------------------------------------------------------


def _fake_registry(services: Services, entries: list[SwitchableModel]) -> None:
    """Serve a fixed list through the real registry's own parsing.

    Feeding fake `systemctl` output rather than replacing the methods keeps the discovery path under
    test, and means no test can probe or start a real model service.
    """
    units = "\n".join(f"{entry.service} {entry.unit_state} enabled" for entry in entries)
    shows = "\n".join(
        f"Id={entry.service}\nExecStart={{ path=/srv/{entry.service}.sh ; }}" for entry in entries
    )
    scripts = {
        f"/srv/{entry.service}.sh": (
            f"vllm serve --served-model-name {entry.model_id}"
            + (f" --max-model-len {entry.max_model_len}" if entry.max_model_len else "")
            + (f" --max-num-seqs {entry.max_concurrency}" if entry.max_concurrency else "")
        )
        for entry in entries
    }
    permitted = ", ".join(
        f"/usr/bin/systemctl start {entry.service.removesuffix('.service')}"
        for entry in entries
        if entry.available
    )

    def runner(argv: Sequence[str]) -> str:
        if "list-unit-files" in argv:
            return units
        if argv[:2] == ["systemctl", "show"]:
            return shows
        return f"    (ALL) NOPASSWD: {permitted}" if permitted else "no NOPASSWD here"

    services.model_registry = ServiceModelRegistry(
        runner=runner, read_text=lambda path: scripts[path]
    )


_AVAILABLE = SwitchableModel(
    model_id="Qwen3.6_35B_A3B_NVFP4",
    service="qwen35-nvfp4.service",
    unit_state="linked",
    available=True,
)
_BLOCKED = SwitchableModel(
    model_id="Masked_Model",
    service="masked.service",
    unit_state="masked",
    available=False,
    unavailable_reason="unit is masked",
)


def test_models_lists_switchable_entries_with_their_reasons(
    client: TestClient, services: Services
) -> None:
    _fake_registry(services, [_AVAILABLE, _BLOCKED])
    body = client.get("/models").json()

    by_id = {entry["model_id"]: entry for entry in body["switchable"]}
    assert by_id["Qwen3.6_35B_A3B_NVFP4"]["available"] is True
    assert by_id["Masked_Model"]["available"] is False
    assert by_id["Masked_Model"]["unavailable_reason"] == "unit is masked"
    assert body["switch"]["status"] == "idle"


def test_switch_rejects_a_model_this_machine_cannot_serve(
    client: TestClient, services: Services
) -> None:
    _fake_registry(services, [_AVAILABLE])
    response = client.post("/models/switch", json={"model_id": "Nope"})
    assert response.status_code == 404


def test_switch_rejects_an_unstartable_model_with_its_reason(
    client: TestClient, services: Services
) -> None:
    _fake_registry(services, [_BLOCKED])
    response = client.post("/models/switch", json={"model_id": "Masked_Model"})
    assert response.status_code == 409
    assert "unit is masked" in response.text


def test_switch_refuses_while_a_run_is_active(client: TestClient, services: Services) -> None:
    """Starting a unit stops the resident model, so an in-flight run would lose it mid-phase."""
    _fake_registry(services, [_AVAILABLE])
    started = _start(client)

    response = client.post("/models/switch", json={"model_id": _AVAILABLE.model_id})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert started["run_id"] in detail["runs"]
    assert services.model_switcher.state.status == "idle", "a refused switch must not start"


def test_switch_proceeds_when_forced(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(services, [_AVAILABLE])
    _start(client)
    switched: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        services.model_switcher,
        "start",
        lambda model, forced=False: (
            switched.append((model.model_id, forced)),
            ModelSwitchState(status="switching", model_id=model.model_id, forced=forced),
        )[1],
    )

    response = client.post("/models/switch", json={"model_id": _AVAILABLE.model_id, "force": True})
    assert response.status_code == 202
    assert response.json()["status"] == "switching"
    assert switched == [(_AVAILABLE.model_id, True)]


def test_unload_rejects_a_model_this_machine_cannot_serve(
    client: TestClient, services: Services
) -> None:
    _fake_registry(services, [_AVAILABLE])

    response = client.post("/models/unload", json={"model_id": "Nope"})

    assert response.status_code == 404


def test_unload_refuses_while_a_run_is_active(client: TestClient, services: Services) -> None:
    _fake_registry(services, [_AVAILABLE])
    started = _start(client)

    response = client.post("/models/unload", json={"model_id": _AVAILABLE.model_id})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert started["run_id"] in detail["runs"]
    assert services.model_switcher.state.status == "idle"


def test_unload_proceeds_when_forced(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(services, [_AVAILABLE])
    _start(client)
    unloaded: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        services.model_switcher,
        "unload",
        lambda model, forced=False: (
            unloaded.append((model.model_id, forced)),
            ModelSwitchState(status="unloading", model_id=model.model_id, forced=forced),
        )[1],
    )

    response = client.post("/models/unload", json={"model_id": _AVAILABLE.model_id, "force": True})

    assert response.status_code == 202
    assert response.json()["status"] == "unloading"
    assert unloaded == [(_AVAILABLE.model_id, True)]
