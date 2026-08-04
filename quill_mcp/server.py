"""Fire-and-forget MCP tools for Quill.

Every tool performs bounded HTTP requests and returns. There is deliberately no SSE subscription,
wait primitive, or polling loop: Quill runs independently and a later agent rediscovers it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from quill.client import ClientError, QuillClient, detect_branch, detect_repo

DEFAULT_SERVER = "http://127.0.0.1:8002"

ClientFactory = Callable[[str], QuillClient]
_client_factory: ClientFactory = QuillClient

mcp = FastMCP(
    "quill",
    instructions=(
        "Start Quill work and return immediately. Never wait for completion; use quill_status or "
        "quill_recent_runs when the user returns later."
    ),
)


def _server() -> str:
    return os.environ.get("QUILL_SERVER", DEFAULT_SERVER).rstrip("/")


def _path(repo_path: str) -> Path:
    path = Path(repo_path).expanduser().resolve()
    if not path.is_dir():
        raise ClientError(f"repository path does not exist or is not a directory: {path}")
    return path


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "run_id",
        "status",
        "repo",
        "branch",
        "ticket",
        "mode",
        "phase",
        "phase_label",
        "attempt",
        "max_attempts",
        "queue_position",
        "pr_url",
        "question",
        "error",
        "queued_at",
        "started_at",
        "updated_at",
    )
    return {key: run[key] for key in keys if key in run}


@mcp.tool()
def quill_start(
    repo_path: str,
    ticket: int,
    mode: Literal["create", "update", "review"] = "create",
    workflow: str = "ticket",
    branch: str | None = None,
) -> dict[str, Any]:
    """Queue a Quill ticket; every run starts with a cleared vLLM prefix cache."""
    if ticket <= 0:
        raise ValueError("ticket must be a positive integer")
    directory = _path(repo_path)
    repo = detect_repo(directory)
    with _client_factory(_server()) as client:
        target = branch or (
            client.review_target(repo, ticket) if mode == "review" else detect_branch(directory)
        )
        started = client.start(
            repo=repo,
            branch=target,
            ticket=ticket,
            mode=mode,
            workflow=workflow,
        )
    return {
        "run_id": started.run_id,
        "status": started.status,
        "queue_position": started.queue_position,
        "repo": repo,
        "branch": target,
        "ticket": ticket,
        "mode": mode,
        "workflow": workflow,
    }


@mcp.tool()
def quill_status(
    run_id: str | None = None,
    repo_path: str | None = None,
    ticket: int | None = None,
) -> dict[str, Any]:
    """Read one run now, by durable ID or newest run matching a local repo and ticket."""
    if run_id is None and repo_path is None:
        raise ValueError("provide run_id or repo_path")
    if run_id is not None and repo_path is not None:
        raise ValueError("provide run_id or repo_path, not both")
    with _client_factory(_server()) as client:
        selected = run_id
        if selected is None:
            repo = detect_repo(_path(repo_path or ""))
            matches = client.runs(repo=repo, ticket=ticket, limit=1)
            if not matches:
                suffix = f" and ticket {ticket}" if ticket is not None else ""
                raise ClientError(f"no runs found for {repo}{suffix}")
            selected = str(matches[0]["run_id"])
        run = client.status(selected)
        artifacts = client.artifacts(selected)
    result = _summary(run)
    result["history"] = run.get("history", [])
    result["artifacts"] = artifacts
    return result


@mcp.tool()
def quill_run_breakdown(run_id: str) -> dict[str, Any]:
    """Return compact ordered stats for every phase call, including retries and rejections."""
    if not run_id.strip():
        raise ValueError("run_id is required")
    with _client_factory(_server()) as client:
        return client.breakdown(run_id)


@mcp.tool()
def quill_recent_runs(
    repo_path: str | None = None,
    ticket: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """List recent runs now so work can be rediscovered in a later agent session."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    repo = detect_repo(_path(repo_path)) if repo_path is not None else None
    with _client_factory(_server()) as client:
        runs = client.runs(repo=repo, ticket=ticket, limit=limit)
    return {"runs": [_summary(run) for run in runs]}


@mcp.tool()
def quill_queue() -> dict[str, Any]:
    """Return the active run and ordered wait queue now; do not watch or poll it."""
    with _client_factory(_server()) as client:
        queue = client.queue()
    active = queue.get("active")
    queued = queue.get("queued", [])
    return {
        "active": _summary(active) if isinstance(active, dict) else None,
        "queued": [_summary(run) for run in queued if isinstance(run, dict)],
        "depth": queue.get("depth", 0),
    }


@mcp.tool()
def quill_stop(run_id: str) -> dict[str, Any]:
    """Stop a queued run immediately or an active run at its next phase boundary."""
    if not run_id.strip():
        raise ValueError("run_id is required")
    with _client_factory(_server()) as client:
        return _summary(client.stop(run_id))


@mcp.tool()
def quill_restart(run_id: str, phase: str) -> dict[str, Any]:
    """Restart an eligible failed/halted new-ticket run from a saved phase boundary."""
    if not run_id.strip() or not phase.strip():
        raise ValueError("run_id and phase are required")
    with _client_factory(_server()) as client:
        return _summary(client.restart(run_id, phase))


@mcp.tool()
def quill_answer(run_id: str, answer: str) -> dict[str, Any]:
    """Answer a run that is currently waiting in needs_decision."""
    if not run_id.strip() or not answer.strip():
        raise ValueError("run_id and answer are required")
    with _client_factory(_server()) as client:
        return _summary(client.answer(run_id, answer))


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
