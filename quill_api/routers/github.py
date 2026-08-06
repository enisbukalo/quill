"""GitHub-backed repository and ticket choices for browser run submission."""

from __future__ import annotations

import base64
import json
import subprocess
from typing import cast

from fastapi import APIRouter, HTTPException, status

from quill.preflight import gh_authenticated, gh_available
from quill.config import ConfigError, load_config_text
from quill.git_ops import (
    AmbiguousPullRequest,
    GitError,
    GitOps,
    SubprocessRunner,
    pr_target_for_repo,
)
from quill_api.deps import ServicesDep
from quill_api.schemas import (
    GitHubIssue,
    GitHubIssueList,
    GitHubIssueTitles,
    GitHubRepository,
    GitHubRepositoryList,
    UpdateTarget,
    WorkflowChoice,
    WorkflowChoiceList,
    WorkflowPhaseChoice,
)
from quill_api.workspace import WorkspaceError, WorkspaceNotFound, validate_repo

router = APIRouter(prefix="/github", tags=["github"])


def _ensure_gh() -> None:
    if not (gh_available() and gh_authenticated()):
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="GitHub CLI (gh) is not installed or authenticated; run `gh auth login`.",
        )


def _gh(*args: str) -> object:
    try:
        completed = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub CLI request failed: {exc}",
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "GitHub CLI request failed."
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub CLI returned invalid JSON.",
        ) from exc


@router.get("/repositories")
def repositories(services: ServicesDep) -> GitHubRepositoryList:
    """Cached source repositories whose default branch contains ``quillfolio.toml``.

    A stale snapshot kicks off a background rescan before answering. Discovery otherwise ran
    exactly once per service start, so a repository added — or access to one granted — after
    boot stayed invisible until someone restarted the service, with no way to tell from the UI
    that the list was simply old. The response still comes from the cache, so this never blocks
    on GitHub; the newer list arrives on the next poll.
    """
    _ensure_gh()
    services.repositories.refresh_if_stale()
    viewer = _gh("api", "user")
    if not isinstance(viewer, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub user is missing."
        )
    viewer_data = cast(dict[str, object], viewer)
    login_value = viewer_data.get("login")
    if not isinstance(login_value, str):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub user is missing."
        )
    login = login_value
    entries = [
        GitHubRepository(
            name=item.name,
            visibility=item.visibility,
            updated_at=item.updated_at,
            default_branch=item.default_branch,
            config_sha=item.config_sha,
            project_board=item.project_board,
        )
        for item in services.repositories.repositories
    ]
    return GitHubRepositoryList(
        login=login,
        repositories=entries,
        scanned_at=services.repositories.scanned_at,
        error=services.repositories.error,
    )


@router.post("/repositories/refresh")
def refresh_repositories(services: ServicesDep) -> GitHubRepositoryList:
    """Refresh configured repositories without cloning or mutating a workspace."""
    _ensure_gh()
    try:
        services.repositories.refresh()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return repositories(services)


@router.get("/repositories/{owner}/{name}/workflows")
def workflows(owner: str, name: str, services: ServicesDep) -> WorkflowChoiceList:
    """Named workflows from the repository's remote default-branch config."""
    _ensure_gh()
    repo = validate_repo(f"{owner}/{name}")
    registered = next(
        (item for item in services.repositories.repositories if item.name == repo), None
    )
    if registered is None:
        raise HTTPException(status_code=404, detail=f"{repo} is not a configured Quill repository")
    payload = _gh(
        "api",
        f"repos/{repo}/contents/quillfolio.toml",
        "-X",
        "GET",
        "-f",
        f"ref={registered.default_branch}",
    )
    encoded = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(encoded, str):
        raise HTTPException(status_code=502, detail="GitHub returned no quillfolio.toml content")
    try:
        text = base64.b64decode(encoded.replace("\n", ""), validate=True).decode("utf-8")
        config = load_config_text(
            text,
            directory=services.workspaces.path_for(repo),
            personas_root=services.settings.personas_root,
            runs_root=services.settings.runs_root,
            vllm_url=services.settings.vllm_url,
            source=f"{repo}@{registered.default_branch}:quillfolio.toml",
        )
    except (ValueError, UnicodeError, ConfigError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    available_models = set(config.vllm_models)
    choices = []
    for item in config.workflows.values():
        phases = []
        for phase in item.phases:
            configured_models = phase.models or tuple(audit.model for audit in phase.audits)
            available_models.update(configured_models)
            if configured_models:
                phases.append(
                    WorkflowPhaseChoice(
                        id=phase.id,
                        label=phase.label or phase.id,
                        model=configured_models[0],
                        parallel_group=phase.parallel_group,
                    )
                )
        choices.append(WorkflowChoice(id=item.id, label=item.label, mode=item.mode, phases=phases))
    return WorkflowChoiceList(
        repo=repo,
        default=config.workflow_id,
        excluded_issue_labels=list(config.excluded_issue_labels),
        workflows=choices,
        models=sorted(available_models),
    )


@router.get("/repositories/{owner}/{name}/issues/{ticket}/update-target")
def update_target(
    owner: str,
    name: str,
    ticket: int,
    services: ServicesDep,
    require_feedback: bool = True,
) -> UpdateTarget:
    """Resolve an update target locally or a read-only review target directly from GitHub."""
    repo = validate_repo(f"{owner}/{name}")
    if not require_feedback:
        try:
            pr = pr_target_for_repo(SubprocessRunner("/tmp"), repo, ticket)
        except AmbiguousPullRequest as exc:
            return UpdateTarget(available=False, reason=str(exc))
        except GitError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if pr is None:
            return UpdateTarget(available=False, reason=f"No open PR found for ticket #{ticket}.")
        return UpdateTarget(
            available=True,
            pr_number=pr.number,
            title=pr.title,
            branch=pr.branch,
            url=pr.url,
            head_sha=pr.head_sha,
            committed_at=pr.committed_at,
            local_branch=False,
        )
    try:
        local = services.workspaces.local_branches(repo)
    except WorkspaceNotFound:
        return UpdateTarget(available=False, reason="This repository has no local checkout yet.")
    git = GitOps(SubprocessRunner(str(services.workspaces.path_for(repo))))
    try:
        pr = git.pr_target_for_ticket(ticket)
    except AmbiguousPullRequest as exc:
        return UpdateTarget(available=False, reason=str(exc))
    except GitError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if pr is None:
        return UpdateTarget(available=False, reason=f"No open PR found for ticket #{ticket}.")
    if pr.branch not in local:
        return UpdateTarget(
            available=False,
            reason=f"PR #{pr.number} branch '{pr.branch}' is not present in the local checkout.",
            pr_number=pr.number,
            branch=pr.branch,
            url=pr.url,
            head_sha=pr.head_sha,
            committed_at=pr.committed_at,
        )
    snapshot = git.feedback_snapshot(pr)
    reason = (
        None
        if snapshot.selected
        else f"No PR feedback was created or edited after {pr.head_sha[:12]}."
    )
    return UpdateTarget(
        available=bool(snapshot.selected),
        reason=reason,
        pr_number=pr.number,
        title=pr.title,
        branch=pr.branch,
        url=pr.url,
        head_sha=pr.head_sha,
        committed_at=pr.committed_at,
        feedback_count=len(snapshot.selected),
        local_branch=True,
    )


@router.get("/repositories/{owner}/{name}/issues")
def issues(owner: str, name: str) -> GitHubIssueList:
    """Open tickets and their repository-defined labels for branch work types."""
    _ensure_gh()
    try:
        repo = validate_repo(f"{owner}/{name}")
    except WorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    raw_issues = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,labels",
    )
    raw_labels = _gh("label", "list", "--repo", repo, "--limit", "100", "--json", "name")
    if not isinstance(raw_issues, list) or not isinstance(raw_labels, list):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub issue metadata is invalid."
        )
    parsed_issues = sorted(
        (
            _issue(cast(dict[str, object], item))
            for item in cast(list[object], raw_issues)
            if isinstance(item, dict)
        ),
        key=lambda issue: issue.number,
    )
    return GitHubIssueList(
        repo=repo,
        issues=parsed_issues,
        work_types=sorted(
            {
                str(cast(dict[str, object], item).get("name", "")).strip().lower()
                for item in cast(list[object], raw_labels)
                if isinstance(item, dict)
                and str(cast(dict[str, object], item).get("name", "")).strip()
            }
        ),
    )


@router.get("/repositories/{owner}/{name}/issue-titles")
def issue_titles(owner: str, name: str, services: ServicesDep) -> GitHubIssueTitles:
    """Issue titles for naming runs after their tickets, including closed issues.

    ``/issues`` lists open issues only, which is exactly the wrong set here: a finished run's
    ticket is normally closed, so joining against that endpoint would leave the runs worth reading
    unnamed. This reads the board watcher's cached repository-wide hierarchy instead, so it costs
    no extra GitHub calls and covers every ticket a run could reference.
    """
    try:
        repo = validate_repo(f"{owner}/{name}")
    except WorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    titles = services.project_queue.issue_titles(repo)
    return GitHubIssueTitles(repo=repo, titles={str(k): v for k, v in titles.items()})


def _issue(item: dict[str, object]) -> GitHubIssue:
    labels = item.get("labels")
    names = (
        [
            str(cast(dict[str, object], label)["name"]).lower()
            for label in cast(list[object], labels)
            if isinstance(label, dict) and "name" in label
        ]
        if isinstance(labels, list)
        else []
    )
    number = item.get("number")
    return GitHubIssue(
        number=number if isinstance(number, int) else 0,
        title=str(item.get("title", "")),
        labels=names,
    )
