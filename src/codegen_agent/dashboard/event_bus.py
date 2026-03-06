"""Simple async pub/sub event bus for real-time WebSocket updates."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []
        self._drops: int = 0  # cumulative dropped events (queue full)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    @property
    def dropped_events(self) -> int:
        """Total number of events dropped due to full subscriber queues."""
        return self._drops

    async def publish(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": event_type,
            "t": time.time(),
            "data": data or {},
        }
        if project_id:
            event["project_id"] = project_id
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._drops += 1
                log.warning(
                    "EventBus: queue full — dropped event '%s' (total drops: %d)",
                    event_type,
                    self._drops,
                )


bus = EventBus()
