"""Drive a remote quill server from a local checkout (server milestone D1).

``quill 42 --server http://quill-box:8002`` derives ``owner/name`` from the repo's ``origin``
remote, submits its branch and ticket, and streams the run's events back. The server loads the
``quillfolio.toml`` committed in its own checkout of that repository.

The terminal output is identical to a local run because the console already sits behind the
``on_event`` callback: the same renderer consumes events whether they came from the engine in this
process or over SSE from another machine.

Only the client half lives here — no models, no personas, no git beyond reading a remote URL. That
is the point: a machine driving a remote server needs none of the stack.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx

from quill.events import Event

#: Terminal event types: once one arrives the run is over and the stream can be dropped.
_TERMINAL = frozenset({"run_done", "run_failed", "run_halted", "needs_decision"})

#: Long enough to sit through a model load or a CI wait without the client giving up first.
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
_REQUEST_TIMEOUT = 30.0

_REMOTE_RE = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


class ClientError(RuntimeError):
    """The remote run could not be started or followed. The message is user-facing."""


@dataclass(frozen=True, slots=True)
class RemoteRun:
    run_id: str
    status: str
    queue_position: int | None


def detect_repo(directory: str | Path) -> str:
    """``owner/name`` from the checkout's ``origin`` remote.

    Derived rather than configured: the client is already standing in the repo it wants shipped, so
    asking the user to name it again is a chance to name it wrong.
    """
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ClientError(
            f"could not read the 'origin' remote in {directory} — is this a git repository "
            "with a remote?"
        ) from exc
    match = _REMOTE_RE.search(out)
    if match is None:
        raise ClientError(f"could not parse an owner/name out of the origin remote: {out!r}")
    return f"{match['owner']}/{match['name']}"


def detect_branch(directory: str | Path) -> str:
    """The checkout's current branch — the branch the server should work on."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ClientError(f"could not read the current branch in {directory}") from exc
    if not branch or branch == "HEAD":
        raise ClientError("the checkout is in a detached HEAD state; check out a branch first.")
    return branch


class QuillClient:
    """Talks to a quill server."""

    def __init__(self, server: str, *, client: httpx.Client | None = None) -> None:
        self.server = server.rstrip("/")
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT)
        self._owns_client = client is None

    def start(
        self,
        *,
        repo: str,
        branch: str,
        ticket: int,
        mode: str,
        workflow: str = "ticket",
        clear_prefix_cache: bool = False,
    ) -> RemoteRun:
        """Queue a run; return its id and queue position."""
        try:
            response = self._client.post(
                f"{self.server}/runs",
                json={
                    "repo": repo,
                    "branch": branch,
                    "ticket": ticket,
                    "mode": mode,
                    "workflow": workflow,
                    "clear_prefix_cache": clear_prefix_cache,
                },
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"could not reach {self.server}: {exc}") from exc
        if response.status_code >= 400:
            raise ClientError(f"{self.server} refused the run: {_detail(response)}")
        body = response.json()
        return RemoteRun(
            run_id=body["run_id"],
            status=body["status"],
            queue_position=body.get("queue_position"),
        )

    def follow(self, run_id: str) -> Iterator[Event]:
        """Yield this run's events until it reaches a terminal state.

        The stream carries every run's events (one service, many repos), so it is filtered to the
        requested id here rather than making the server hold a per-client subscription.
        """
        try:
            with self._client.stream(
                "GET", f"{self.server}/events", timeout=_STREAM_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    raise ClientError(f"could not open the event stream: {response.status_code}")
                for line in response.iter_lines():
                    event = _parse_sse(line)
                    if event is None or event.get("run_id") not in (run_id, None):
                        continue
                    yield event
                    if event.get("type") in _TERMINAL:
                        return
        except httpx.HTTPError as exc:
            raise ClientError(f"lost the event stream: {exc}") from exc

    def status(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/runs/{run_id}", action=f"read run {run_id}")

    def runs(
        self,
        *,
        repo: str | None = None,
        ticket: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest-first run summaries, optionally filtered."""
        params: dict[str, str | int] = {"limit": limit}
        if repo is not None:
            params["repo"] = repo
        if ticket is not None:
            params["ticket"] = ticket
        if status is not None:
            params["status"] = status
        body = self._request("GET", "/runs", action="list runs", params=params)
        runs = body.get("runs")
        if not isinstance(runs, list):
            raise ClientError(f"{self.server} returned an invalid run list")
        return [run for run in runs if isinstance(run, dict)]

    def queue(self) -> dict[str, Any]:
        return self._request("GET", "/queue", action="read queue")

    def review_target(self, repo: str, ticket: int) -> str:
        """Resolve the current open PR branch for a read-only review run."""
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name:
            raise ClientError(f"invalid repository name: {repo!r}")
        target = self._request(
            "GET",
            f"/github/repositories/{owner}/{name}/issues/{ticket}/update-target",
            action=f"resolve PR review target for {repo}#{ticket}",
            params={"require_feedback": "false"},
        )
        branch = target.get("branch")
        if target.get("available") is not True or not isinstance(branch, str) or not branch.strip():
            reason = target.get("reason")
            detail = reason if isinstance(reason, str) and reason.strip() else "no open PR found"
            raise ClientError(f"could not resolve PR review target for {repo}#{ticket}: {detail}")
        return branch

    def stop(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/runs/{run_id}/stop", action=f"stop run {run_id}")

    def restart(self, run_id: str, phase: str) -> dict[str, Any]:
        """Start a linked run from a durable phase checkpoint."""
        return self._request(
            "POST",
            f"/runs/{run_id}/restart",
            action=f"restart run {run_id} from {phase}",
            json={"phase": phase},
        )

    def answer(self, run_id: str, answer: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/runs/{run_id}/decision",
            action=f"answer run {run_id}",
            json={"answer": answer},
        )

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        body = self._request(
            "GET", f"/runs/{run_id}/artifacts", action=f"list artifacts for {run_id}"
        )
        artifacts = body.get("artifacts")
        if not isinstance(artifacts, list):
            raise ClientError(f"{self.server} returned an invalid artifact list")
        return [artifact for artifact in artifacts if isinstance(artifact, dict)]

    def breakdown(self, run_id: str) -> dict[str, Any]:
        """Complete persisted timing, usage, transcript, and tool-call telemetry."""
        return self._request(
            "GET", f"/runs/{run_id}/breakdown", action=f"read breakdown for {run_id}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method, f"{self.server}{path}", params=params, json=json
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"could not {action} at {self.server}: {exc}") from exc
        if response.status_code >= 400:
            raise ClientError(f"could not {action}: {_detail(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ClientError(
                f"{self.server} returned invalid JSON while trying to {action}"
            ) from exc
        if not isinstance(body, dict):
            raise ClientError(
                f"{self.server} returned an invalid response while trying to {action}"
            )
        return body

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def run_remote(
    *,
    server: str,
    directory: str,
    ticket: int,
    mode: str,
    branch: str | None,
    on_event: Callable[[Event], None],
    workflow: str = "ticket",
    clear_prefix_cache: bool = False,
) -> Event:
    """Start a run on ``server`` and stream it to ``on_event``; return the final event."""
    repo = detect_repo(directory)
    target = branch or detect_branch(directory)

    with QuillClient(server) as client:
        started = client.start(
            repo=repo,
            branch=target,
            ticket=ticket,
            mode=mode,
            workflow=workflow,
            clear_prefix_cache=clear_prefix_cache,
        )
        if started.queue_position:
            on_event(
                {
                    "type": "queued",
                    "run_id": started.run_id,
                    "position": started.queue_position,
                }
            )
        final: Event = {"type": "run_failed", "reason": "the event stream ended unexpectedly"}
        for event in client.follow(started.run_id):
            on_event(event)
            final = event
        return final


def _parse_sse(line: str) -> Event | None:
    """One ``data:`` line from an SSE stream as an event, or None for keep-alives and comments."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _detail(response: httpx.Response) -> str:
    """The server's error message, falling back to the raw body."""
    try:
        body = response.json()
    except ValueError:
        return f"{response.status_code} {response.text.strip()}"
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error")
        if detail:
            return f"{response.status_code} {detail}"
    return f"{response.status_code} {response.text.strip()}"
