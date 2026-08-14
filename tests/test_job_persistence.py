"""Tests for job state surviving a restart.

Restart is simulated the only way that matters: build a store, mutate it, throw
it away, and build a new one over the same directory — exactly what happens when
the container comes back up.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from batch.models import JobSummary
from batch.store import JobsStore
from services.state_mirror import JsonStateMirror
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec, VideoJobState


# --- the mirror itself -----------------------------------------------------


def test_state_survives_a_round_trip(tmp_path: Path) -> None:
    mirror = JsonStateMirror(tmp_path, VideoJobState, lambda s: s.job_id)
    state = VideoJobState(job_id="a", status="polling")
    state.summary.heygen_video_id = "video-1"

    mirror.write(state)
    loaded = JsonStateMirror(tmp_path, VideoJobState, lambda s: s.job_id).load()

    assert loaded["a"].summary.heygen_video_id == "video-1"
    assert loaded["a"].status == "polling"


def test_writes_are_atomic(tmp_path: Path) -> None:
    """A crash mid-write must leave the previous good state, not a truncated
    file — so no .tmp file is left behind and the target is always complete."""
    mirror = JsonStateMirror(tmp_path, VideoJobState, lambda s: s.job_id)

    mirror.write(VideoJobState(job_id="a", status="queued"))
    mirror.write(VideoJobState(job_id="a", status="completed"))

    assert list(tmp_path.glob("*.json.tmp")) == []
    assert mirror.load()["a"].status == "completed"


def test_one_corrupt_file_does_not_stop_the_others_loading(tmp_path: Path) -> None:
    """A single bad record must not stop the process starting."""
    mirror = JsonStateMirror(tmp_path, VideoJobState, lambda s: s.job_id)
    mirror.write(VideoJobState(job_id="good", status="completed"))
    (tmp_path / "broken.json").write_text("{not json at all")

    loaded = mirror.load()

    assert list(loaded) == ["good"]


def test_an_empty_directory_loads_nothing(tmp_path: Path) -> None:
    assert JsonStateMirror(tmp_path, VideoJobState, lambda s: s.job_id).load() == {}


# --- video jobs ------------------------------------------------------------


def _video_spec() -> VideoJobSpec:
    return VideoJobSpec(script="hello", video_title="T", publish_date="12-08-2026")


def test_a_video_job_interrupted_mid_poll_becomes_recoverable(tmp_path: Path) -> None:
    """The hole this closes: a job killed while polling used to reload as
    'polling' forever — never terminal, so never in list_recoverable, so its
    finished and already-paid-for render was stranded."""
    async def _before():
        store = VideoJobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.set_spec("job-1", _video_spec())
        await store.patch_summary("job-1", heygen_video_id="video-1")
        await store.set_status("job-1", "polling", "Polling render status")

    asyncio.run(_before())

    reloaded = VideoJobsStore(persist_dir=tmp_path)
    state = asyncio.run(reloaded.get("job-1"))

    assert state.status == "failed"
    assert "Interrupted by a restart" in state.error
    assert asyncio.run(reloaded.list_recoverable()) == ["job-1"]
    # The render id and spec survive, which is what recovery needs.
    assert state.summary.heygen_video_id == "video-1"
    assert state.spec.video_title == "T"


@pytest.mark.parametrize(
    "status", ["queued", "tts", "uploading", "generating", "polling", "downloading", "nas_upload"]
)
def test_every_in_flight_video_status_is_settled(tmp_path: Path, status: str) -> None:
    async def _before():
        store = VideoJobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.set_status("job-1", status)

    asyncio.run(_before())

    state = asyncio.run(VideoJobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.status == "failed"


def test_a_completed_video_job_is_left_alone(tmp_path: Path) -> None:
    async def _before():
        store = VideoJobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.complete("job-1")

    asyncio.run(_before())

    state = asyncio.run(VideoJobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.status == "completed"
    assert state.error is None


def test_an_already_failed_video_job_keeps_its_original_error(tmp_path: Path) -> None:
    async def _before():
        store = VideoJobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.fail("job-1", "HeyGen render failed: bad image")

    asyncio.run(_before())

    state = asyncio.run(VideoJobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.error == "HeyGen render failed: bad image"


def test_without_a_persist_dir_nothing_is_written(tmp_path: Path) -> None:
    async def _go():
        store = VideoJobsStore()
        await store.create("job-1")

    asyncio.run(_go())

    assert list(tmp_path.iterdir()) == []


# --- audio batch jobs ------------------------------------------------------


def test_an_interrupted_audio_batch_reports_how_far_it_got(tmp_path: Path) -> None:
    """An audio batch cannot resume — its work is in memory — but it should say
    what happened instead of vanishing into a 404."""
    async def _before():
        store = JobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.start("job-1")
        summary = JobSummary(total_rows=40, rows_processed=12, rows_succeeded=12)
        await store.update_summary("job-1", summary)

    asyncio.run(_before())

    state = asyncio.run(JobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.status == "failed"
    assert state.error == "Interrupted by a restart"
    assert state.summary.rows_processed == 12
    assert state.summary.total_rows == 40


def test_an_interrupted_audio_batch_keeps_its_download_links(tmp_path: Path) -> None:
    """The zips for completed activities are already on disk, so these links
    still work — the one thing an audio batch really can recover."""
    from batch.models import ArchiveDownload

    archive = ArchiveDownload(
        activity_name="act1", language="Hindi", filename="act1-Hindi.zip",
        path="/tmp/act1-Hindi.zip", url="/output/batch_archives/act1-Hindi.zip",
        reason="s3_disabled",
    )

    async def _before():
        store = JobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.start("job-1")
        summary = JobSummary(local_archives_succeeded=1, archive_downloads=[archive])
        await store.update_summary("job-1", summary)

    asyncio.run(_before())

    state = asyncio.run(JobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.status == "failed"
    assert state.summary.archive_downloads[0].url == "/output/batch_archives/act1-Hindi.zip"


def test_a_completed_audio_batch_is_left_alone(tmp_path: Path) -> None:
    async def _before():
        store = JobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.start("job-1")
        await store.complete("job-1", JobSummary(total_rows=2, rows_succeeded=2))

    asyncio.run(_before())

    state = asyncio.run(JobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.status == "completed"
    assert state.summary.rows_succeeded == 2


def test_a_cancelled_audio_batch_stays_cancelled(tmp_path: Path) -> None:
    async def _before():
        store = JobsStore(persist_dir=tmp_path)
        await store.create("job-1")
        await store.start("job-1")
        await store.cancel("job-1")

    asyncio.run(_before())

    state = asyncio.run(JobsStore(persist_dir=tmp_path).get("job-1"))

    assert state.status == "cancelled"


# --- video batch jobs ------------------------------------------------------


def _rows(n: int) -> list[BatchRowState]:
    return [BatchRowState(row_index=i, script=f"s{i}", video_title=f"T{i}") for i in range(2, 2 + n)]


def test_an_interrupted_video_batch_settles_its_unfinished_rows(tmp_path: Path) -> None:
    async def _before():
        store = VideoBatchJobsStore(persist_dir=tmp_path)
        await store.create("batch-1", _rows(3))
        await store.start("batch-1")
        await store.update_row("batch-1", 2, status="completed", nas_path="12-08-2026/T2.mp4")
        await store.row_succeeded("batch-1")
        await store.update_row("batch-1", 3, status="running", job_id="job-x")

    asyncio.run(_before())

    state = asyncio.run(VideoBatchJobsStore(persist_dir=tmp_path).get("batch-1"))

    assert state.status == "partial"
    by_index = {r.row_index: r for r in state.rows}
    assert by_index[2].status == "completed"
    assert by_index[2].nas_path == "12-08-2026/T2.mp4"
    assert by_index[3].status == "failed"
    assert by_index[4].status == "failed"
    assert state.failed_count == 2


def test_a_video_batch_interrupted_before_any_row_finished_is_failed(tmp_path: Path) -> None:
    async def _before():
        store = VideoBatchJobsStore(persist_dir=tmp_path)
        await store.create("batch-1", _rows(2))
        await store.start("batch-1")

    asyncio.run(_before())

    state = asyncio.run(VideoBatchJobsStore(persist_dir=tmp_path).get("batch-1"))

    assert state.status == "failed"
    assert all(row.status == "failed" for row in state.rows)


def test_an_interrupted_video_batch_keeps_its_row_job_ids(tmp_path: Path) -> None:
    """Each row records the video job that rendered it, and those jobs remain
    independently recoverable through VideoJobsStore."""
    async def _before():
        store = VideoBatchJobsStore(persist_dir=tmp_path)
        await store.create("batch-1", _rows(1))
        await store.start("batch-1")
        await store.update_row("batch-1", 2, status="running", job_id="job-abc")

    asyncio.run(_before())

    state = asyncio.run(VideoBatchJobsStore(persist_dir=tmp_path).get("batch-1"))

    assert state.rows[0].job_id == "job-abc"


def test_a_completed_video_batch_is_left_alone(tmp_path: Path) -> None:
    async def _before():
        store = VideoBatchJobsStore(persist_dir=tmp_path)
        await store.create("batch-1", _rows(1))
        await store.start("batch-1")
        await store.update_row("batch-1", 2, status="completed")
        await store.row_succeeded("batch-1")
        await store.complete("batch-1")

    asyncio.run(_before())

    state = asyncio.run(VideoBatchJobsStore(persist_dir=tmp_path).get("batch-1"))

    assert state.status == "completed"
    assert state.failed_count == 0
