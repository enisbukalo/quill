"""HTTP routers, one module per resource.

Each router owns its own prefix and tags so mounting is a single `include_router(router)` with no
per-call configuration to drift.
"""

from __future__ import annotations

from quill_api.routers.catalog import personas_router, skills_router
from quill_api.routers.events import router as events_router
from quill_api.routers.github import router as github_router
from quill_api.routers.memories import router as memories_router
from quill_api.routers.project_queue import router as project_queue_router
from quill_api.routers.runs import router as runs_router
from quill_api.routers.system import router as system_router
from quill_api.routers.workspaces import router as workspaces_router

__all__ = [
    "events_router",
    "github_router",
    "memories_router",
    "personas_router",
    "project_queue_router",
    "runs_router",
    "skills_router",
    "system_router",
    "workspaces_router",
]
