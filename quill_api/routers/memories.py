"""Operator surface for repository-scoped verified blocker memories."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from quill.blocker_memory import count_memory_events, delete_memories, list_verified_memories
from quill_api.deps import ServicesDep
from quill_api.schemas import (
    DeleteMemoriesRequest,
    DeleteMemoriesResult,
    MemoryEntry,
    MemoryList,
)

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("")
def list_memories(services: ServicesDep) -> MemoryList:
    return MemoryList(
        memories=[
            MemoryEntry(
                memory_id=record.memory_id,
                repo=record.repo,
                finding=record.finding,
                phases=list(record.phases),
                occurrences=record.occurrences,
                last_verified_at=record.last_verified_at,
                changed_files=list(record.changed_files),
            )
            for record in list_verified_memories(services.settings.memory_root)
        ],
        archived_events=count_memory_events(services.settings.memory_root),
    )


@router.delete("")
def remove_memories(body: DeleteMemoriesRequest, services: ServicesDep) -> DeleteMemoriesResult:
    if not body.delete_all and not body.memory_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provide memory_ids or set delete_all=true",
        )
    return DeleteMemoriesResult(
        deleted=delete_memories(
            services.settings.memory_root,
            memory_ids=set(body.memory_ids),
            delete_all=body.delete_all,
        )
    )
