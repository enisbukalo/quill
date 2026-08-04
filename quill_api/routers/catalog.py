"""Persona and skill libraries: list, read, and edit.

Every mutation requires a ``reason`` and is committed and pushed to the config repo holding the
library (see :mod:`quill_api.catalog_git`). Without that, a shared library would accumulate
anonymous edits with no record of what changed or why.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from quill_api import catalog
from quill_api.catalog_git import CatalogConflict, CatalogRepo, CommitResult, commit_message
from quill_api.deps import ServicesDep
from quill_api.schemas import (
    CatalogEntryInfo,
    CatalogList,
    DeleteRequest,
    FileWrite,
    PersonaCreate,
    PersonaDetail,
    PersonaWrite,
    SkillCreate,
    SkillDetail,
    SkillWrite,
    WriteResult,
)

personas_router = APIRouter(prefix="/personas", tags=["personas"])
skills_router = APIRouter(prefix="/skills", tags=["skills"])


def _info(entry: catalog.CatalogEntry) -> CatalogEntryInfo:
    return CatalogEntryInfo(name=entry.name, description=entry.description, suits=entry.suits)


def _commit(repo: CatalogRepo, paths: list[Path], message: str, *, name: str) -> WriteResult:
    """Commit a catalog change, translating the graded failure policy into HTTP."""
    try:
        result: CommitResult = repo.commit_and_push(paths, message)
    except CatalogConflict as exc:
        # The commit exists locally; only the rebase failed. 409 so the caller knows the edit is
        # safe but the remote needs a human.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return WriteResult(
        name=name,
        path=str(paths[0].name if paths else ""),
        committed=result.committed,
        pushed=result.pushed,
        sha=result.sha,
        error=result.error,
    )


def _handle(exc: catalog.CatalogError) -> HTTPException:
    if isinstance(exc, catalog.NotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, catalog.AlreadyExists):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, catalog.LibraryUnwritable):
        # 500, not 400: the request was fine, the server's library root is not.
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# -- personas ---------------------------------------------------------------------


@personas_router.get("")
def list_personas(services: ServicesDep) -> CatalogList:
    root = services.settings.personas_root
    return CatalogList(root=str(root), entries=[_info(e) for e in catalog.discover_personas(root)])


@personas_router.get("/{name}")
def read_persona(name: str, services: ServicesDep) -> PersonaDetail:
    try:
        entry, body = catalog.read_persona(services.settings.personas_root, name)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    return PersonaDetail(
        name=entry.name, description=entry.description, suits=entry.suits, body=body
    )


@personas_router.post("", status_code=status.HTTP_201_CREATED)
def create_persona(body: PersonaCreate, services: ServicesDep) -> WriteResult:
    root = services.settings.personas_root
    try:
        path = catalog.write_persona(root, body.name, body.body, create=True)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("personas", body.name, "add", path.name, body.reason)
    return _commit(services.personas, [path], message, name=body.name)


@personas_router.put("/{name}")
def update_persona(name: str, body: PersonaWrite, services: ServicesDep) -> WriteResult:
    root = services.settings.personas_root
    try:
        path = catalog.write_persona(root, name, body.body, create=False)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("personas", name, "update", path.name, body.reason)
    return _commit(services.personas, [path], message, name=name)


@personas_router.delete("/{name}")
def delete_persona(name: str, body: DeleteRequest, services: ServicesDep) -> WriteResult:
    root = services.settings.personas_root
    try:
        path = catalog.delete_persona(root, name)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("personas", name, "remove", path.name, body.reason)
    return _commit(services.personas, [path], message, name=name)


# -- skills -----------------------------------------------------------------------


@skills_router.get("")
def list_skills(services: ServicesDep) -> CatalogList:
    root = services.settings.skills_root
    return CatalogList(root=str(root), entries=[_info(e) for e in catalog.discover_skills(root)])


@skills_router.get("/{name}")
def read_skill(name: str, services: ServicesDep) -> SkillDetail:
    try:
        entry, body, files = catalog.read_skill(services.settings.skills_root, name)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    return SkillDetail(name=entry.name, description=entry.description, body=body, files=files)


@skills_router.post("", status_code=status.HTTP_201_CREATED)
def create_skill(body: SkillCreate, services: ServicesDep) -> WriteResult:
    root = services.settings.skills_root
    try:
        paths = catalog.write_skill(root, body.name, body.body, create=True, files=body.files)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("skills", body.name, "add", catalog.SKILL_FILENAME, body.reason)
    return _commit(services.skills, paths, message, name=body.name)


@skills_router.put("/{name}")
def update_skill(name: str, body: SkillWrite, services: ServicesDep) -> WriteResult:
    root = services.settings.skills_root
    try:
        paths = catalog.write_skill(root, name, body.body, create=False, files=body.files)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("skills", name, "update", catalog.SKILL_FILENAME, body.reason)
    return _commit(services.skills, paths, message, name=name)


@skills_router.delete("/{name}")
def delete_skill(name: str, body: DeleteRequest, services: ServicesDep) -> WriteResult:
    root = services.settings.skills_root
    try:
        path = catalog.delete_skill(root, name)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("skills", name, "remove", "skill", body.reason)
    return _commit(services.skills, [path], message, name=name)


@skills_router.get("/{name}/files/{relative:path}")
def read_skill_file(name: str, relative: str, services: ServicesDep) -> FileWrite:
    """One auxiliary file's content. Skills like box3d carry a whole wiki/ beside SKILL.md."""
    try:
        path = catalog.skill_file(services.settings.skills_root, name, relative)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no file {relative!r} in {name!r}"
        )
    return FileWrite(content=path.read_text(encoding="utf-8"), reason="read")


@skills_router.put("/{name}/files/{relative:path}")
def write_skill_file(
    name: str, relative: str, body: FileWrite, services: ServicesDep
) -> WriteResult:
    root = services.settings.skills_root
    try:
        path = catalog.write_skill_file(root, name, relative, body.content)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("skills", name, "update", relative, body.reason)
    return _commit(services.skills, [path], message, name=name)


@skills_router.delete("/{name}/files/{relative:path}")
def delete_skill_file(
    name: str, relative: str, body: DeleteRequest, services: ServicesDep
) -> WriteResult:
    root = services.settings.skills_root
    try:
        path = catalog.delete_skill_file(root, name, relative)
    except catalog.CatalogError as exc:
        raise _handle(exc) from exc
    message = commit_message("skills", name, "remove", relative, body.reason)
    return _commit(services.skills, [path], message, name=name)
