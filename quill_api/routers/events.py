"""Server-sent events: the live transition stream every client watches.

This is what makes a remote run feel local — the CLI feeds these straight into the same console
renderer it uses when driving a pipeline itself.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from quill_api.deps import ServicesDep

router = APIRouter(tags=["events"])


@router.get("/events")
async def events_stream(request: Request, services: ServicesDep) -> EventSourceResponse:
    """Every run's events, tagged with ``run_id`` so a client can filter to its own."""
    bus = services.bus

    async def generate() -> AsyncIterator[dict[str, object]]:
        async for event in bus.subscribe(services.live_sync()):
            if await request.is_disconnected():
                break
            yield {"data": json.dumps(event), "id": str(event.get("revision", ""))}

    return EventSourceResponse(generate())
