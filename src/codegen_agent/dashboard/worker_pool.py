"""Bounded async worker pool — global semaphore + per-project semaphores."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

from .config import cfg

log = logging.getLogger(__name__)


class WorkerPool:
    """Global semaphore + per-project semaphores.

    Projects borrow up to per_project_max slots from the global budget.
    Prevents runaway parallel execution when many projects are queued.
    """

    def __init__(self) -> None:
        self._global_sem = asyncio.Semaphore(cfg.workers.global_max)
        self._project_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(cfg.workers.per_project_max)
        )
        self._active_counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def run(
        self,
        project_id: str,
        coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    ) -> Any:
        proj_sem = self._project_sems[project_id]
        async with self._global_sem:
            async with proj_sem:
                async with self._lock:
                    self._active_counts[project_id] += 1
                try:
                    return await coro_fn()
                finally:
                    async with self._lock:
                        self._active_counts[project_id] -= 1

    async def run_many(
        self,
        project_id: str,
        coro_fns: list[Callable[[], Coroutine[Any, Any, Any]]],
    ) -> list[Any]:
        tasks = [asyncio.create_task(self.run(project_id, fn)) for fn in coro_fns]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def active_count(self, project_id: str) -> int:
        return self._active_counts.get(project_id, 0)

    def total_active(self) -> int:
        return sum(self._active_counts.values())


pool = WorkerPool()
