"""The job queue: how many audio jobs (Excel sheets and stories) run at once.

Every audio job is handed here instead of being spawned directly. At most
``limit`` run at a time (``AUDIO_MAX_CONCURRENT_JOBS``, default 3); the rest
wait first come, first served, across both kinds. A waiting job stays
``queued`` and carries its 1-based queue position on ``JobState``, recomputed
for every waiter whenever the queue changes.

A slot is held for exactly as long as the runner coroutine runs: it is freed in
a ``finally`` when the runner returns or raises, not by watching statuses. The
runners record their own terminal status (completed / failed / cancelled).

When a job's turn comes and it is no longer ``queued``, or a cancel was
requested while it waited (which is then recorded), it is skipped and its
runner is never created.

Work is still launched through ``api.background.spawn``, so it keeps the strong
reference and the logging of escaped exceptions.

Loop safety: the waiting line and slot count are kept per running event loop,
like ``services.concurrency``'s semaphore. In the app there is one loop, so one
queue; in tests each loop gets a fresh one, and nothing left by a finished loop
can hold a slot in the next.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
import weakref
from typing import Any, Awaitable, Callable

from api.background import spawn
from batch.store import JobsStore
from services.concurrency import audio_max_concurrent_jobs

logger = logging.getLogger(__name__)

MakeCoro = Callable[[], Awaitable[Any]]


@dataclass
class _Waiting:
    job_id: str
    make_coro: MakeCoro
    name: str


@dataclass
class _Lane:
    """One loop's queue: who is waiting, and how many slots are taken."""

    waiting: deque[_Waiting] = field(default_factory=deque)
    running: int = 0
    # Serialises dispatch, so two jobs ending together cannot both take the
    # last slot, and position writes land in the order they were computed.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class JobQueue:
    def __init__(self, jobs_store: JobsStore, *, limit: int | None = None) -> None:
        """``limit`` fixes the slot count (for tests); None reads
        ``AUDIO_MAX_CONCURRENT_JOBS`` each time a slot is considered."""
        self._store = jobs_store
        self._limit = limit
        self._lanes: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Lane]" = (
            weakref.WeakKeyDictionary()
        )

    @property
    def limit(self) -> int:
        return self._limit if self._limit is not None else audio_max_concurrent_jobs()

    def _lane(self) -> _Lane:
        loop = asyncio.get_running_loop()
        lane = self._lanes.get(loop)
        if lane is None:
            lane = _Lane()
            self._lanes[loop] = lane
        return lane

    async def submit(self, job_id: str, make_coro: MakeCoro, *, name: str) -> None:
        """Put a job (already created ``queued`` in the store) in line. It starts
        now if a slot is free. ``make_coro`` is called only when it starts."""
        lane = self._lane()
        lane.waiting.append(_Waiting(job_id, make_coro, name))
        await self._dispatch(lane)

    async def _dispatch(self, lane: _Lane) -> None:
        """Start waiters while slots are free, then renumber those left."""
        async with lane.lock:
            started: list[str] = []
            while lane.waiting and lane.running < self.limit:
                item = lane.waiting.popleft()
                state = await self._store.get(item.job_id)
                if state is None or state.status != "queued":
                    logger.info("Job %s left the queue without running", item.job_id)
                    continue
                if await self._store.is_cancelled(item.job_id):
                    # Cancel was asked for while it waited: it never runs.
                    await self._store.cancel(item.job_id)
                    continue
                lane.running += 1
                started.append(item.job_id)
                spawn(self._run(lane, item), name=item.name)
            positions: dict[str, int | None] = {job_id: None for job_id in started}
            positions.update({w.job_id: n for n, w in enumerate(lane.waiting, start=1)})
            await self._store.set_queue_positions(positions)

    async def _run(self, lane: _Lane, item: _Waiting) -> None:
        try:
            await item.make_coro()
        finally:
            lane.running -= 1
            await self._dispatch(lane)
