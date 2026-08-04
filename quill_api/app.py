"""FastAPI service — owns the runs, REST + SSE (WI-10 / WI-12).

The API is both a human dashboard and a developer-agent surface: everything needed to drive,
observe, and diagnose a run is reachable over HTTP. Live `RunState` is in-process; run summaries
hit SQLite.

This module is now just assembly — lifespan, error handling, and mounting routers. The behaviour
lives in :mod:`quill_api.services` and :mod:`quill_api.routers`, so adding an endpoint does not
mean growing a single function that already knows about everything.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from quill import __version__
from quill_api.routers import (
    events_router,
    github_router,
    memories_router,
    personas_router,
    project_queue_router,
    runs_router,
    skills_router,
    system_router,
    workspaces_router,
)
from quill_api.services import Services
from quill_api.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    services: Services = app.state.services
    services.bus.bind_loop(asyncio.get_running_loop())
    services.telemetry.bind_loop(asyncio.get_running_loop())
    services.start()
    try:
        yield
    finally:
        services.stop()


def create_app(services: Services | None = None) -> FastAPI:
    app = FastAPI(
        title="quill-api",
        version=__version__,
        lifespan=lifespan,
        summary="Run quill pipelines for any repo, from any machine.",
    )
    app.state.services = services or Services()
    web_root = app.state.services.settings.web_root

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Structured JSON errors everywhere — never bare 500 text (WI-12), so a client can branch
        # on failures instead of scraping strings.
        return JSONResponse(
            status_code=500, content={"error": "internal_error", "detail": str(exc)}
        )

    for router in (
        system_router,
        project_queue_router,
        runs_router,
        personas_router,
        skills_router,
        events_router,
        github_router,
        memories_router,
        workspaces_router,
    ):
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(web_root / "index.html")

    app.mount("/assets", StaticFiles(directory=web_root), name="assets")
    return app


def run() -> None:
    """`quill-api` console-script entry point.

    Passes the **factory** rather than a module-level ``app``. Constructing `Services` opens the
    history database and creates the state directories, and a module-level instance would do that
    on mere *import* — including when the test suite imports this module, which would leave a
    `~/.quill` on any machine that ran the tests. Nothing touches the filesystem until a server is
    actually asked for.

    Serve it directly with ``uvicorn quill_api.app:create_app --factory``.
    """
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "quill_api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
