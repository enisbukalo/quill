"""GitHub Project ticket candidates and durable batch execution order."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from quill.git_ops import GitError
from quill.preflight import gh_authenticated, gh_available
from quill_api.deps import ServicesDep
from quill_api.repository_registry import ConfiguredRepository
from quill_api.schemas import (
    AddProjectQueueBatchRequest,
    ProjectQueueBatchResult,
    ProjectQueueCandidate,
    ProjectQueueCandidateGroup,
    ProjectQueueCandidates,
    ProjectQueueView,
)
from quill_api.workspace import WorkspaceError, validate_repo


router = APIRouter(prefix="/project-queue", tags=["project queue"])


@router.get("")
def project_queue(services: ServicesDep) -> ProjectQueueView:
    """Return durable ticket batches in their actual execution order."""
    return services.project_queue.view()


@router.get("/{owner}/{name}/candidates")
def project_queue_candidates(
    owner: str, name: str, services: ServicesDep
) -> ProjectQueueCandidates:
    """Return selectable Project issues grouped under native parent epics."""
    _ensure_gh()
    repository = _repository(services, owner, name)
    try:
        catalog = services.project_queue.catalog(repository)
    except (GitError, OSError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ProjectQueueCandidates(
        repo=repository.name,
        project_board=repository.project_board or "",
        groups=[
            ProjectQueueCandidateGroup(
                epic_number=group.epic_number,
                epic_title=group.epic_title,
                tickets=[
                    ProjectQueueCandidate(
                        number=item.number,
                        title=item.title,
                        labels=list(item.labels),
                        status=item.status,
                        selectable=(
                            item.selectable
                            and services.history.find_active_project_queue_item(
                                repository.name, item.number
                            )
                            is None
                        ),
                        reason=_candidate_reason(services, repository.name, item),
                    )
                    for item in group.tickets
                ],
            )
            for group in catalog.groups
            if group.tickets
        ],
    )


@router.post("/{owner}/{name}", status_code=status.HTTP_202_ACCEPTED)
def add_project_queue_batch(
    owner: str,
    name: str,
    body: AddProjectQueueBatchRequest,
    services: ServicesDep,
) -> ProjectQueueBatchResult:
    """Move selected tickets to Queue and create one durable FIFO batch."""
    _ensure_gh()
    repository = _repository(services, owner, name)
    try:
        return services.project_queue.add_batch(repository, body.tickets)
    except (GitError, OSError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _repository(services: ServicesDep, owner: str, name: str) -> ConfiguredRepository:
    try:
        repo = validate_repo(f"{owner}/{name}")
    except WorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    matches = [item for item in services.repositories.repositories if item.name == repo]
    if not matches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{repo} is not a configured Quill repository",
        )
    repository = matches[0]
    if repository.project_board is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{repo} does not configure [repo].project_board",
        )
    return repository


def _candidate_reason(services: ServicesDep, repo: str, item: object) -> str | None:
    number = getattr(item, "number", None)
    selectable = getattr(item, "selectable", False)
    status_value = getattr(item, "status", "")
    if isinstance(number, int):
        active = services.history.find_active_project_queue_item(repo, number)
        if active is not None:
            return f"already belongs to queue batch {active.batch_id}"
    if not selectable:
        return f"Project status is {status_value or 'unset'}"
    return None


def _ensure_gh() -> None:
    if not (gh_available() and gh_authenticated()):
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="GitHub CLI (gh) is not installed or not authenticated; run `gh auth login`.",
        )
