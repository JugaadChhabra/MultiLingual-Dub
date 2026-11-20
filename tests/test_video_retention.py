"""Retention (video): a sweep removes the folders of video jobs that ended long
enough ago, and orphan job-id folders past the window, and nothing else. Job
records always survive. Driven with an explicit "now"; the loop's error
handling is driven with a fake sleep.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.video_pipeline import retention
from services.video_pipeline.retention import sweep_expired_job_folders, video_retention_loop
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobState, VideoJobSummary

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(hours=48)


def _hours_ago(n: float) -> datetime:
    return NOW - timedelta(hours=n)


def _id() -> str:
    return uuid.uuid4().hex


def _seed(persist: Path, job_id: str, status: str, *, finished: datetime | None) -> None:
    """Write a job file as the store's mirror would, so a fresh store loads it.
    In-flight jobs would be settled on load, so those go through the store."""
    state = VideoJobState(job_id=job_id, status=status,
                          summary=VideoJobSummary(finished_at=finished))
    (persist / f"{job_id}.json").write_text(state.model_dump_json())


def _folder(root: Path, name: str, *, mtime: datetime | None = None) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "render.mp4").write_bytes(b"video")
    if mtime is not None:
        os.utime(folder, (mtime.timestamp(), mtime.timestamp()))
    return folder


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "heygen"
    persist = root / "_jobs"
    persist.mkdir(parents=True)
    ids = {key: _id() for key in (
        "old_completed", "old_failed", "old_cancelled",
        "recent_completed", "recent_failed", "recent_cancelled",
        "old_in_flight", "old_recovering", "no_finish",
        "old_orphan", "recent_orphan",
    )}
    for status in ("completed", "failed", "cancelled"):
        _seed(persist, ids[f"old_{status}"], status, finished=_hours_ago(72))
        _seed(persist, ids[f"recent_{status}"], status, finished=_hours_ago(2))
    _seed(persist, ids["no_finish"], "cancelled", finished=None)
    # An old failed job claimed for Recovery: it keeps its old finish time.
    _seed(persist, ids["old_recovering"], "failed", finished=_hours_ago(200))

    for key in ids:
        if key.endswith("orphan"):
            continue
        _folder(root, ids[key])
    _folder(root, ids["old_orphan"], mtime=_hours_ago(72))
    _folder(root, ids["recent_orphan"], mtime=_hours_ago(2))
    # Not job-id shaped, or app state: never touched, however old.
    keep = [
        _folder(root, "notes", mtime=_hours_ago(500)),
        _folder(root, "ABCDEF0123456789ABCDEF0123456789", mtime=_hours_ago(500)),
        _folder(root, "_library", mtime=_hours_ago(500)),
        _folder(root, "_audio_cache", mtime=_hours_ago(500)),
    ]
    top_file = root / ("f" * 32)
    top_file.write_bytes(b"stray")
    os.utime(top_file, (_hours_ago(500).timestamp(),) * 2)
    keep.append(top_file)

    store = VideoJobsStore(persist_dir=persist)

    async def _in_flight():
        await store.create(ids["old_in_flight"])
        await store.set_status(ids["old_in_flight"], "polling")
        await store.patch_summary(ids["old_in_flight"], finished_at=_hours_ago(300))
        await store.set_status(ids["old_recovering"], "recovering")

    asyncio.run(_in_flight())
    return root, persist, store, ids, keep


def _all_ids(ids: dict[str, str]) -> list[str]:
    return [v for k, v in ids.items() if not k.endswith("orphan")]


def test_a_sweep_removes_exactly_the_expired_folders_and_no_records(world) -> None:
    root, persist, store, ids, keep = world
    records_before = sorted(p.name for p in persist.iterdir())

    removed = asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))

    gone = {"old_completed", "old_failed", "old_cancelled", "old_orphan"}
    assert sorted(removed) == sorted(ids[k] for k in gone)
    for key, job_id in ids.items():
        assert (root / job_id).exists() is (key not in gone), key
    for path in keep:
        assert path.exists()
    assert (persist).is_dir()
    assert sorted(p.name for p in persist.iterdir()) == records_before
    for job_id in _all_ids(ids):
        assert asyncio.run(store.get(job_id)) is not None
    assert asyncio.run(store.get(ids["old_recovering"])).status == "recovering"
    assert asyncio.run(store.get(ids["old_in_flight"])).status == "polling"


def test_every_job_record_survives_a_reload(world) -> None:
    root, persist, store, ids, _ = world
    asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))

    reloaded = VideoJobsStore(persist_dir=persist)

    for job_id in _all_ids(ids):
        assert asyncio.run(reloaded.get(job_id)) is not None


def test_a_folder_already_gone_is_fine(world) -> None:
    root, _persist, store, ids, _ = world
    (root / ids["old_completed"] / "render.mp4").unlink()
    (root / ids["old_completed"]).rmdir()

    removed = asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))

    assert ids["old_completed"] not in removed
    assert ids["old_failed"] in removed


def test_a_folder_that_cannot_be_deleted_is_logged_and_left(world, monkeypatch, caplog) -> None:
    root, _persist, store, ids, _ = world
    monkeypatch.setattr(retention.shutil, "rmtree", lambda *_a, **_k: None)

    with caplog.at_level(logging.WARNING, logger="services.video_pipeline.retention"):
        removed = asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))

    assert removed == []
    assert (root / ids["old_completed"]).exists()
    assert any("could not delete" in r.getMessage() for r in caplog.records)


def test_a_record_whose_id_points_outside_the_output_folder_is_skipped(tmp_path) -> None:
    root = tmp_path / "heygen"
    persist = root / "_jobs"
    persist.mkdir(parents=True)
    outside = _folder(tmp_path, "outside")
    (root / ("a" * 32)).symlink_to(outside)
    for job_id in ("_library", "a" * 32):
        _seed(persist, job_id, "completed", finished=_hours_ago(72))
    library = _folder(root, "_library")
    store = VideoJobsStore(persist_dir=persist)

    removed = asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))

    assert removed == []
    assert library.exists()
    assert (outside / "render.mp4").exists()


def test_a_record_whose_id_is_not_job_id_shaped_keeps_its_folder(tmp_path) -> None:
    root = tmp_path / "heygen"
    persist = root / "_jobs"
    persist.mkdir(parents=True)
    _seed(persist, "notes", "completed", finished=_hours_ago(72))
    notes = _folder(root, "notes")
    store = VideoJobsStore(persist_dir=persist)

    assert asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW)) == []
    assert notes.exists()


def test_a_job_claimed_by_recovery_mid_sweep_keeps_its_folder(world, monkeypatch) -> None:
    root, _persist, store, ids, _ = world
    real = retention._candidates

    def listing_then_recovery_claims(*args):
        found = real(*args)
        # Recovery claims the job after the sweep listed it, before deletion.
        store._jobs[ids["old_failed"]].status = "recovering"
        return found

    monkeypatch.setattr(retention, "_candidates", listing_then_recovery_claims)

    removed = asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))

    assert ids["old_failed"] not in removed
    assert (root / ids["old_failed"] / "render.mp4").exists()
    assert ids["old_completed"] in removed


def test_a_naive_finish_time_is_read_as_utc(tmp_path) -> None:
    root = tmp_path / "heygen"
    persist = root / "_jobs"
    persist.mkdir(parents=True)
    job_id = _id()
    _seed(persist, job_id, "completed", finished=_hours_ago(72).replace(tzinfo=None))
    _folder(root, job_id)
    store = VideoJobsStore(persist_dir=persist)

    assert asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW)) == [job_id]


def test_a_sweep_logs_a_one_line_total(world, caplog) -> None:
    root, _persist, store, _ids, _ = world
    with caplog.at_level(logging.INFO, logger="services.video_pipeline.retention"):
        asyncio.run(sweep_expired_job_folders(store, root, WINDOW, NOW))
    totals = [r.getMessage() for r in caplog.records
              if "Video retention sweep removed" in r.getMessage()]
    assert len(totals) == 1
    assert "removed 4 job folder(s) (1 orphan)" in totals[0]


def test_a_failing_sweep_is_logged_and_the_loop_keeps_going(monkeypatch, tmp_path, caplog) -> None:
    calls: list[int] = []

    async def broken_sweep(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(retention, "sweep_expired_job_folders", broken_sweep)
    sleeps: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    with caplog.at_level(logging.ERROR, logger="services.video_pipeline.retention"):
        with pytest.raises(Stop):
            asyncio.run(video_retention_loop(VideoJobsStore(), tmp_path, sleep=fake_sleep))

    assert len(calls) == 3
    assert sleeps == [retention.SWEEP_INTERVAL_SECONDS] * 3
    assert sum("Video retention sweep failed" in r.getMessage() for r in caplog.records) == 3


def test_the_window_comes_from_the_environment(monkeypatch) -> None:
    from services.concurrency import video_output_retention_hours

    monkeypatch.delenv("VIDEO_OUTPUT_RETENTION_HOURS", raising=False)
    assert video_output_retention_hours() == 48
    monkeypatch.setenv("VIDEO_OUTPUT_RETENTION_HOURS", "1.5")
    assert video_output_retention_hours() == 1.5
    monkeypatch.setenv("VIDEO_OUTPUT_RETENTION_HOURS", "-3")
    assert video_output_retention_hours() == 48


def test_a_huge_window_sweeps_nothing_rather_than_failing(world, monkeypatch, caplog) -> None:
    root, _persist, store, ids, _ = world
    monkeypatch.setenv("VIDEO_OUTPUT_RETENTION_HOURS", "inf")

    with caplog.at_level(logging.ERROR, logger="services.video_pipeline.retention"):
        asyncio.run(retention.sweep_once(store, root))

    assert not caplog.records
    assert (root / ids["old_completed"]).exists()
