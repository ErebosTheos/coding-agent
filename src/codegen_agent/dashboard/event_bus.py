"""Simple async pub/sub event bus for real-time WebSocket updates."""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

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
                pass


bus = EventBus()
