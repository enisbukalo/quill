"""Operator administration of the server's persistent per-repo checkouts.

Distinct from ``/github/*``, which describes the authenticated GitHub *account*: this router
describes and mutates the clones that live on *this* server. Branch deletion here is deliberately
local-only — removing an origin branch is a materially different, destructive remote operation and
is not implied by "delete branches".

Every mutation first refuses to run while a queued or executing pipeline targets the same repo, so
an operator can never delete or move a branch out from under a run that is about to prepare it.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, status

from quill_api.deps import ServicesDep
from quill_api.schemas import (
    WorkspaceBranchInfo,
    WorkspaceBranchList,
    WorkspaceInfo,
    WorkspaceList,
    WorkspaceMutationResult,
)
from quill_api.services import Services
from quill_api.workspace import (
    WorkspaceConflict,
    WorkspaceError,
    WorkspaceGitError,
    WorkspaceMutation,
    WorkspaceNotFound,
    validate_repo,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _repo(owner: str, name: str) -> str:
    """Validate ``owner/name`` up front, turning a malformed identifier into a friendly 422."""
    try:
        return validate_repo(f"{owner}/{name}")
    except WorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _guard_no_active_run(services: Services, repo: str) -> None:
    """Refuse a mutation while any unfinished run targets ``repo``.

    Both the currently executing checkout and a still-queued run matter: a queued run's requested
    branch could otherwise be deleted or moved before the worker prepares it. Reads are unaffected.
    """
    blocking = next(
        (run for run in services.store.all() if run.is_active and run.repo == repo), None
    )
    if blocking is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{repo} has an active run ({blocking.run_id}, {blocking.status.value}); "
                f"workspace changes are blocked until it finishes."
            ),
        )


def _run_mutation(action: Callable[[], WorkspaceMutation]) -> WorkspaceMutation:
    """Invoke a manager branch operation, mapping its typed errors to HTTP status codes."""
    try:
        return action()
    except WorkspaceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except WorkspaceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except WorkspaceGitError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except WorkspaceError as exc:
        # Bare WorkspaceError only comes from ref validation inside the manager: bad input, 422.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("")
def list_workspaces(services: ServicesDep) -> WorkspaceList:
    """Every persistent checkout on this server and the branch each is sitting on."""
    return WorkspaceList(
        workspaces=[
            WorkspaceInfo(repo=checkout.repo, branch=checkout.branch)
            for checkout in services.workspaces.checkouts()
        ]
    )


@router.get("/{owner}/{name}/branches")
def list_branches(owner: str, name: str, services: ServicesDep) -> WorkspaceBranchList:
    """Fetched, merged local + remote branch choices for one checkout."""
    repo = _repo(owner, name)
    try:
        branches = services.workspaces.branches(repo)
    except WorkspaceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except WorkspaceGitError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except WorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return WorkspaceBranchList(
        repo=repo,
        current=next((b.name for b in branches if b.current), None),
        branches=[
            WorkspaceBranchInfo(name=b.name, current=b.current, local=b.local, remote=b.remote)
            for b in branches
        ],
    )


@router.post("/{owner}/{name}/branches/{branch:path}/pull")
def pull_branch(
    owner: str, name: str, branch: str, services: ServicesDep
) -> WorkspaceMutationResult:
    """Fetch and fast-forward ``branch`` in the checkout. Refused while a run targets the repo."""
    repo = _repo(owner, name)
    _guard_no_active_run(services, repo)
    result = _run_mutation(lambda: services.workspaces.pull_branch(repo, branch))
    return WorkspaceMutationResult(repo=result.repo, branch=result.branch, message=result.message)


@router.delete("/{owner}/{name}/branches/{branch:path}")
def delete_branch(
    owner: str, name: str, branch: str, services: ServicesDep
) -> WorkspaceMutationResult:
    """Delete only the local ``branch`` ref. origin is preserved; refused while a run targets it."""
    repo = _repo(owner, name)
    _guard_no_active_run(services, repo)
    result = _run_mutation(lambda: services.workspaces.delete_branch(repo, branch))
    return WorkspaceMutationResult(repo=result.repo, branch=result.branch, message=result.message)
