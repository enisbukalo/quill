"""Asyncio event bus — fan-out of driver events to SSE subscribers (WI-9).

The driver runs in a worker thread and emits events through its `on_event` callback. The
API wires that callback to :meth:`EventBus.publish_threadsafe`, which marshals each event
back onto the event loop and fans it out to every open `/events` subscriber.

Each subscriber gets its own bounded `asyncio.Queue`; `subscribe()` is an async generator
yielding events, suitable for an SSE response. The bus keeps no history — late subscribers
see only events published after they subscribe (run history lives in SQLite, WI-10).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from quill.events import Event

type LiveMessage = dict[str, object]

_QUEUE_MAXSIZE = 1000


class EventBus:
    """Fan-out asyncio event bus. One queue per subscriber."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._subscribers: set[asyncio.Queue[LiveMessage]] = set()
        self._revision = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the running loop (called from app startup) for threadsafe publishing."""
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def is_bound(self) -> bool:
        return self._loop is not None

    def publish(self, event: Event | LiveMessage) -> None:
        """Deliver `event` to every subscriber. Call from the event loop thread.

        A full subscriber queue drops the event for that subscriber (slow-consumer
        protection) rather than blocking the whole fan-out.
        """
        self._revision += 1
        message: LiveMessage = {**event, "revision": self._revision}
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync_required", "revision": self._revision})

    def publish_threadsafe(self, event: Event) -> None:
        """Publish from another thread (the driver worker) onto the bound loop.

        This is the `on_event` callback the API hands the driver.
        """
        if self._loop is None:
            raise RuntimeError("EventBus loop not bound; call bind_loop() at startup")
        self._loop.call_soon_threadsafe(self.publish, event)

    async def subscribe(self, initial: LiveMessage | None = None) -> AsyncIterator[LiveMessage]:
        """Yield events as they are published, until the consumer stops iterating."""
        queue: asyncio.Queue[LiveMessage] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        try:
            if initial is not None:
                yield {**initial, "revision": self._revision}
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
