"""Running a job after the response has gone out.

asyncio only holds a WEAK reference to a running task. A bare
``asyncio.create_task(...)`` whose result nobody keeps can therefore be
garbage-collected mid-flight — the job simply stops, part-way through a render,
with nothing logged. Every background job here goes through ``spawn`` so a
strong reference is held until the task actually finishes.

The second thing this fixes: an exception escaping a background task goes
nowhere. Nothing awaits these, so a raise inside one is discovered only if
Python happens to warn at collection time. ``spawn`` retrieves and logs it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)

# The strong references. A task removes itself once done.
_running: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        logger.info("Background task %s was cancelled", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        # Jobs are expected to record their own failure in their store; reaching
        # here means one escaped, so it would otherwise be invisible.
        logger.error("Background task %s failed: %s", task.get_name(), exc, exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Run ``coro`` in the background, keeping it alive and logging its failures."""
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_on_done)
    return task


def running_count() -> int:
    """How many background jobs are in flight. For tests and diagnostics."""
    return len(_running)
