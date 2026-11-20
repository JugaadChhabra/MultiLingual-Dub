"""Retention: a sweep removes the local zips and job record of audio jobs that
ended long enough ago, and nothing else. Driven with an explicit "now", so
nothing waits on a clock; the loop's error handling is driven with a fake sleep.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from batch import retention
from batch.models import JobState, JobSummary
from batch.retention import retention_loop, sweep_expired_jobs
from batch.store import JobsStore

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(days=14)


def _days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def _seed(persist_dir: Path, job_id: str, status: str, *, finished: datetime | None,
          started: datetime | None = None) -> None:
    """Write a job file as the store's mirror would, so a fresh store loads it.
    A ``running`` job written this way would be settled on load, so in-flight
    jobs are created through the store instead (see the fixture)."""
    state = JobState(
        job_id=job_id, status=status,
        summary=JobSummary(started_at=started, finished_at=finished),
    )
    (persist_dir / f"{job_id}.json").write_text(state.model_dump_json())


def _archive(output: Path, job_id: str) -> Path:
    folder = output / "batch_archives" / job_id / "activity"
    folder.mkdir(parents=True)
    (folder / "hi-IN.zip").write_bytes(b"zip")
    return output / "batch_archives" / job_id


@pytest.fixture
def world(tmp_path):
    output = tmp_path
    persist = output / "batch" / "_jobs"
    persist.mkdir(parents=True)
    _seed(persist, "old_done", "completed", finished=_days_ago(20), started=_days_ago(21))
    _seed(persist, "old_failed", "failed", finished=_days_ago(20))
    _seed(persist, "recent_done", "completed", finished=_days_ago(2))
    _seed(persist, "cancelled_no_finish", "cancelled", finished=None)
    for job_id in ("old_done", "old_failed", "recent_done", "cancelled_no_finish", "running_old"):
        _archive(output, job_id)

    heygen_file = output / "heygen" / "render.mp4"
    heygen_file.parent.mkdir(parents=True)
    heygen_file.write_bytes(b"video")
    heygen_job = output / "heygen" / "_jobs" / "x.json"
    heygen_job.parent.mkdir(parents=True)
    heygen_job.write_text("{}")

    store = JobsStore(persist_dir=persist)

    async def _running():
        await store.create("running_old")
        await store.start("running_old")
        # An old start time: age alone must not get an in-flight job swept.
        summary = (await store.get("running_old")).summary
        summary.started_at = _days_ago(60)
        await store.update_summary("running_old", summary)

    asyncio.run(_running())
    return output, persist, store, [heygen_file, heygen_job]


def test_a_sweep_removes_only_terminal_jobs_finished_before_the_window(world) -> None:
    output, persist, store, heygen = world

    removed = asyncio.run(sweep_expired_jobs(store, output, WINDOW, NOW))

    assert sorted(removed) == ["old_done", "old_failed"]
    for job_id in ("old_done", "old_failed"):
        assert not (output / "batch_archives" / job_id).exists()
        assert not (persist / f"{job_id}.json").exists()
        assert asyncio.run(store.get(job_id)) is None
    for job_id in ("recent_done", "running_old", "cancelled_no_finish"):
        assert (output / "batch_archives" / job_id / "activity" / "hi-IN.zip").exists()
        assert (persist / f"{job_id}.json").exists()
        assert asyncio.run(store.get(job_id)) is not None
    assert asyncio.run(store.get("running_old")).status == "running"
    for path in heygen:
        assert path.exists()


def test_a_removed_job_stays_gone_after_a_restart(world) -> None:
    output, persist, store, _ = world
    asyncio.run(sweep_expired_jobs(store, output, WINDOW, NOW))

    reloaded = JobsStore(persist_dir=persist)

    assert asyncio.run(reloaded.get("old_done")) is None
    assert asyncio.run(reloaded.get("recent_done")) is not None


def test_a_job_with_no_local_archive_is_still_removed(world) -> None:
    output, _persist, store, _ = world
    shutil.rmtree(output / "batch_archives" / "old_done")

    removed = asyncio.run(sweep_expired_jobs(store, output, WINDOW, NOW))

    assert "old_done" in removed
    assert asyncio.run(store.get("old_done")) is None


def test_a_sweep_logs_a_one_line_total(world, caplog) -> None:
    output, _persist, store, _ = world
    with caplog.at_level(logging.INFO, logger="batch.retention"):
        asyncio.run(sweep_expired_jobs(store, output, WINDOW, NOW))
    totals = [r.getMessage() for r in caplog.records if "Retention sweep removed" in r.getMessage()]
    assert len(totals) == 1
    assert "removed 2 audio job(s)" in totals[0]


def test_a_failing_sweep_is_logged_and_the_loop_keeps_going(monkeypatch, tmp_path, caplog) -> None:
    calls: list[int] = []

    async def broken_sweep(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(retention, "sweep_expired_jobs", broken_sweep)
    sleeps: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    with caplog.at_level(logging.ERROR, logger="batch.retention"):
        with pytest.raises(Stop):
            asyncio.run(retention_loop(JobsStore(), tmp_path, sleep=fake_sleep))

    assert len(calls) == 3
    assert sleeps == [retention.SWEEP_INTERVAL_SECONDS] * 3
    assert sum("Retention sweep failed" in r.getMessage() for r in caplog.records) == 3
