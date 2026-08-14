"""Tests for background job spawning.

The defect these guard against is quiet: asyncio holds only a weak reference to
a running task, so a job spawned and forgotten can be collected mid-flight. It
does not raise, it does not log — the work simply stops.
"""
from __future__ import annotations

import asyncio
import gc

import pytest

from api import background


@pytest.fixture(autouse=True)
def clean_registry():
    background._running.clear()
    yield
    background._running.clear()


def test_a_running_task_is_strongly_referenced() -> None:
    """The mechanism that fixes the hazard: while a job runs, the registry holds
    a real reference to it.

    Note what this does NOT claim. asyncio's docs require callers to keep a
    reference because the loop holds only a weak one, but a task that is
    actively scheduled will usually survive a collection anyway — so a test that
    spawns, collects, and asserts completion passes against the buggy code too.
    What is checkable is that the reference exists, and that it is the registry
    holding it rather than luck.
    """
    finished = []

    async def job():
        await asyncio.sleep(0.01)
        finished.append(True)

    async def _go():
        task = background.spawn(job(), name="job-1")
        assert task in background._running
        del task
        gc.collect()
        # Still tracked with no caller-side reference left anywhere.
        assert background.running_count() == 1
        await asyncio.sleep(0.05)

    asyncio.run(_go())

    assert finished == [True]


def test_a_finished_task_is_released() -> None:
    """Holding references forever would be a leak of its own."""
    async def job():
        return None

    async def _go():
        background.spawn(job(), name="job-1")
        assert background.running_count() == 1
        await asyncio.sleep(0.02)
        return background.running_count()

    assert asyncio.run(_go()) == 0


def test_many_tasks_are_tracked_independently() -> None:
    done = []

    async def job(n):
        await asyncio.sleep(0.01)
        done.append(n)

    async def _go():
        for n in range(5):
            background.spawn(job(n), name=f"job-{n}")
        assert background.running_count() == 5
        await asyncio.sleep(0.06)

    asyncio.run(_go())

    assert sorted(done) == [0, 1, 2, 3, 4]
    assert background.running_count() == 0


def test_a_failing_task_is_logged_not_swallowed(caplog) -> None:
    """recover_video_job re-raises after marking a job failed, and nothing
    awaits it — without this the exception vanishes entirely."""
    async def job():
        raise RuntimeError("recovery blew up")

    async def _go():
        background.spawn(job(), name="job-boom")
        await asyncio.sleep(0.02)

    with caplog.at_level("ERROR"):
        asyncio.run(_go())

    assert "recovery blew up" in caplog.text
    assert "job-boom" in caplog.text


def test_one_failing_task_does_not_disturb_the_others() -> None:
    finished = []

    async def ok():
        await asyncio.sleep(0.01)
        finished.append("ok")

    async def boom():
        raise RuntimeError("nope")

    async def _go():
        background.spawn(boom(), name="boom")
        background.spawn(ok(), name="ok")
        await asyncio.sleep(0.05)

    asyncio.run(_go())

    assert finished == ["ok"]
    assert background.running_count() == 0


def test_a_cancelled_task_is_released_without_an_error(caplog) -> None:
    async def job():
        await asyncio.sleep(10)

    async def _go():
        task = background.spawn(job(), name="job-cancelled")
        task.cancel()
        await asyncio.sleep(0.02)

    with caplog.at_level("ERROR"):
        asyncio.run(_go())

    assert background.running_count() == 0
    assert "ERROR" not in caplog.text
