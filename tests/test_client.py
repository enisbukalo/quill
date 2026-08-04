"""Client-mode tests — driving a remote server from a local checkout."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from quill.client import (
    ClientError,
    QuillClient,
    _parse_sse,
    detect_branch,
    detect_repo,
    run_remote,
)
from quill.events import Event

_CONFIG = """\
[runner]
kind = "pi"

[build]
command = "make"
test = "make test"
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "quill@test")
    _git(repo, "config", "user.name", "quill")
    _git(repo, "remote", "add", "origin", "https://github.com/me/proj.git")
    (repo / "quillfolio.toml").write_text(_CONFIG, encoding="utf-8")
    (repo / "README.md").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


# -- local detection --------------------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/me/proj.git",
        "https://github.com/me/proj",
        "git@github.com:me/proj.git",
        "ssh://git@github.com/me/proj.git",
    ],
)
def test_detect_repo_handles_every_remote_form(tmp_path: Path, remote: str) -> None:
    """The client derives owner/name rather than asking, so it must cope with how the remote is
    actually written."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", remote)

    assert detect_repo(repo) == "me/proj"


def test_detect_repo_without_a_remote_errors(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init")
    with pytest.raises(ClientError, match="origin"):
        detect_repo(repo)


def test_detect_branch_reads_the_checkout(checkout: Path) -> None:
    assert detect_branch(checkout) == "main"


def test_detect_branch_refuses_detached_head(checkout: Path) -> None:
    """A detached HEAD has no branch name to give the server."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(checkout, "checkout", sha)

    with pytest.raises(ClientError, match="detached"):
        detect_branch(checkout)


# -- SSE parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [": keep-alive comment", "event: ping", "", "data:", "data: not json", "data: [1, 2]"],
)
def test_parse_sse_ignores_non_events(line: str) -> None:
    assert _parse_sse(line) is None


def test_parse_sse_reads_a_data_payload() -> None:
    assert _parse_sse('data: {"type": "run_done"}') == {"type": "run_done"}


# -- the HTTP surface -------------------------------------------------------------


type _Handler = Callable[[httpx.Request], httpx.Response]


def _transport(handler: _Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_posts_repository_coordinates_and_returns_the_queue_position() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1", "status": "queued", "queue_position": 2})

    with QuillClient("http://box:8002", client=_transport(handler)) as client:
        run = client.start(
            repo="me/proj",
            branch="b",
            ticket=42,
            mode="create",
        )

    assert run.run_id == "r1"
    assert run.queue_position == 2
    assert "config" not in seen
    assert seen["repo"] == "me/proj"
    assert "allow_duplicate" not in seen
    assert seen["workflow"] == "ticket"
    assert seen["clear_prefix_cache"] is False


def test_start_posts_prefix_cache_option() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1", "status": "queued"})

    with QuillClient("http://box", client=_transport(handler)) as client:
        client.start(
            repo="me/proj",
            branch="b",
            ticket=42,
            mode="create",
            clear_prefix_cache=True,
        )
    assert seen["clear_prefix_cache"] is True


def test_start_surfaces_the_servers_error_message() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "branch is invalid"})

    with (
        QuillClient("http://box", client=_transport(handler)) as client,
        pytest.raises(ClientError, match="branch is invalid"),
    ):
        client.start(repo="me/proj", branch="", ticket=1, mode="create")


def test_start_reports_an_unreachable_server() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with (
        QuillClient("http://box", client=_transport(handler)) as client,
        pytest.raises(ClientError, match="could not reach"),
    ):
        client.start(repo="me/proj", branch="b", ticket=1, mode="create")


def test_review_target_resolves_the_open_pr_branch() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.url.query.decode()))
        return httpx.Response(
            200,
            json={"available": True, "branch": "feature/ticket_15", "pr_number": 32},
        )

    with QuillClient("http://box", client=_transport(handler)) as client:
        branch = client.review_target("me/proj", 15)

    assert branch == "feature/ticket_15"
    assert seen == [
        ("/github/repositories/me/proj/issues/15/update-target", "require_feedback=false")
    ]


def test_review_target_rejects_an_unavailable_pr() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available": False, "reason": "No open PR found."})

    with (
        QuillClient("http://box", client=_transport(handler)) as client,
        pytest.raises(ClientError, match="No open PR found"),
    ):
        client.review_target("me/proj", 15)


def test_lifecycle_request_methods_and_filters() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        if request.url.path == "/runs":
            return httpx.Response(200, json={"runs": [{"run_id": "r1"}]})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"active": None, "queued": [], "depth": 0})
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"run_id": "r1", "artifacts": [{"name": "p"}]})
        if request.url.path.endswith("/breakdown"):
            return httpx.Response(200, json={"run_id": "r1", "phases": [{"phase": "impl"}]})
        return httpx.Response(200, json={"run_id": "r1", "status": "halted"})

    with QuillClient("http://box", client=_transport(handler)) as client:
        assert client.runs(repo="me/proj", ticket=7, status="done", limit=3)[0]["run_id"] == "r1"
        assert client.queue()["depth"] == 0
        assert client.status("r1")["run_id"] == "r1"
        assert client.stop("r1")["status"] == "halted"
        assert client.answer("r1", "yes")["run_id"] == "r1"
        assert client.artifacts("r1") == [{"name": "p"}]
        assert client.breakdown("r1")["phases"][0]["phase"] == "impl"

    query = seen[0][2]
    assert "repo=me%2Fproj" in query
    assert "ticket=7" in query
    assert "status=done" in query


def test_request_methods_reject_invalid_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with (
        QuillClient("http://box", client=_transport(handler)) as client,
        pytest.raises(ClientError, match="invalid JSON"),
    ):
        client.queue()


def _sse(*events: dict[str, object]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def test_follow_filters_to_this_run_and_stops_at_a_terminal_event() -> None:
    """One service serves many repos, so the stream carries everyone's events."""
    stream = _sse(
        {"type": "phase_started", "run_id": "other", "phase": "plan"},
        {"type": "phase_started", "run_id": "mine", "phase": "plan"},
        {"type": "run_done", "run_id": "mine"},
        {"type": "phase_started", "run_id": "other", "phase": "impl"},
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream)

    with QuillClient("http://box", client=_transport(handler)) as client:
        received = list(client.follow("mine"))

    assert [e["type"] for e in received] == ["phase_started", "run_done"]
    assert all(e["run_id"] == "mine" for e in received)


# -- end to end -------------------------------------------------------------------


def test_run_remote_streams_events_and_returns_the_final_one(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/runs":
            return httpx.Response(
                202, json={"run_id": "r1", "status": "queued", "queue_position": 0}
            )
        return httpx.Response(
            200,
            content=_sse(
                {"type": "run_started", "run_id": "r1"},
                {"type": "run_done", "run_id": "r1", "pr_url": "u"},
            ),
        )

    monkeypatch.setattr(
        "quill.client.QuillClient",
        lambda server, **_kw: QuillClient(server, client=_transport(handler)),
    )
    seen: list[Event] = []

    final = run_remote(
        server="http://box",
        directory=str(checkout),
        ticket=42,
        mode="create",
        branch=None,
        on_event=seen.append,
    )

    assert final["type"] == "run_done"
    assert [e["type"] for e in seen] == ["run_started", "run_done"]


def test_run_remote_reports_a_queue_position(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/runs":
            return httpx.Response(
                202, json={"run_id": "r1", "status": "queued", "queue_position": 3}
            )
        return httpx.Response(200, content=_sse({"type": "run_done", "run_id": "r1"}))

    monkeypatch.setattr(
        "quill.client.QuillClient",
        lambda server, **_kw: QuillClient(server, client=_transport(handler)),
    )
    seen: list[Event] = []

    run_remote(
        server="http://box",
        directory=str(checkout),
        ticket=42,
        mode="create",
        branch=None,
        on_event=seen.append,
    )

    assert seen[0]["type"] == "queued"
    assert seen[0]["position"] == 3
