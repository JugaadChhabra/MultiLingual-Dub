"""Pause and cancel for a running video job or batch.

Both are cooperative: the pipeline calls ``checkpoint`` between steps, and that
is where a pause holds and a cancel takes effect. A step already under way runs
to its end — except waiting on HeyGen, which ``race`` cuts short on cancel.

What a pause does NOT do is stop a render HeyGen already has: that is paid for
at submission and finishes on their side regardless. A paused batch starts no
new row, and holds rows that have not been submitted yet.

Controls live in memory only. A restart settles every in-flight job and batch
anyway (see the stores' ``_settle``), so there is nothing left to control.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")


class JobCancelled(Exception):
    """Raised at a checkpoint once the operator has cancelled."""

    def __init__(self) -> None:
        super().__init__("Cancelled by the operator")


class JobControl:
    def __init__(self) -> None:
        self._running = asyncio.Event()
        self._running.set()
        self._cancelled = asyncio.Event()

    @property
    def paused(self) -> bool:
        return not self._running.is_set() and not self.cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def pause(self) -> None:
        if not self.cancelled:
            self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._running.set()   # wake anything held by a pause, so it sees the cancel

    async def checkpoint(self) -> None:
        """Hold while paused; raise JobCancelled once cancelled."""
        await self._running.wait()
        if self.cancelled:
            raise JobCancelled()

    async def race(self, aw: Awaitable[T]) -> T:
        """Await ``aw``, abandoning it if the job is cancelled first."""
        work = asyncio.ensure_future(aw)
        stop = asyncio.ensure_future(self._cancelled.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if not work.done():
            work.cancel()
            try:
                await work
            except BaseException:
                pass
            raise JobCancelled()
        return work.result()


class JobControls:
    """Controls by job or batch id, for the routes to find."""

    def __init__(self) -> None:
        self._controls: dict[str, JobControl] = {}

    def open(self, key: str) -> JobControl:
        control = self._controls[key] = JobControl()
        return control

    def get(self, key: str) -> JobControl | None:
        return self._controls.get(key)

    def close(self, key: str) -> None:
        self._controls.pop(key, None)
