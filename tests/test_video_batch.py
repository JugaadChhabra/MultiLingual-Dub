"""Batch-level tests, mostly about talking photo slot sharing.

The bug this guards against: every row re-uploading the same image, tripping the
provider's photo cap and triggering list -> delete -> re-upload churn on every
single render.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.email import EmailSettings
from services.nas import NasConfig
from services.video_pipeline.batch_excel import HeyGenBatchRow
from services.video_pipeline.heygen_client import HeyGenSettings
from services.video_pipeline.batch_runner import run_video_batch_job
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.store import VideoJobsStore
from tests.fakes import FakeRenderer, FakeSpeech


@pytest.fixture
def nas_root(tmp_path: Path) -> Path:
    return tmp_path / "nas"


def _nas(root: Path) -> NasConfig:
    return NasConfig(
        mode="local", root_path=str(root), server="", share="",
        username="", password="", domain="", port=445,
    )


def _heygen() -> HeyGenSettings:
    # Concurrency 1 keeps the recorded call order deterministic.
    return HeyGenSettings(
        api_key="key",
        character_voice_ids={"indian": "voice-abc", "us": "voice-us"},
        batch_concurrency=1,
    )


def _rows(n: int) -> list[HeyGenBatchRow]:
    return [
        HeyGenBatchRow(row_index=i, script=f"script {i}", video_title=f"Title {i}")
        for i in range(2, 2 + n)
    ]


async def _run_batch(
    *,
    tmp_path: Path,
    rows: list[HeyGenBatchRow],
    renderer: FakeRenderer,
    speech: FakeSpeech | None = None,
    slots: TalkingPhotoSlots | None = None,
    nas_root: Path | None = None,
):
    batch_store = VideoBatchJobsStore()
    await batch_store.create(
        "batch-1",
        [BatchRowState(row_index=r.row_index, script=r.script, video_title=r.video_title) for r in rows],
    )
    await run_video_batch_job(
        batch_id="batch-1",
        rows=rows,
        image_bytes=b"\xff\xd8fake-jpeg",
        image_filename="photo.jpg",
        character="indian",
        motion_prompt=None,
        publish_date="12-08-2026",
        output_dir=tmp_path / "out" / "heygen",
        output_base_dir=tmp_path / "out",
        batch_store=batch_store,
        video_jobs_store=VideoJobsStore(),
        renderer=renderer,
        speech=speech or FakeSpeech(),
        slots=slots if slots is not None else TalkingPhotoSlots(renderer),
        heygen=_heygen(),
        nas_config=_nas(nas_root or (tmp_path / "nas")),
        email=EmailSettings(api_key="", from_address="", to_addresses=()),
    )
    return await batch_store.get("batch-1")


def test_a_batch_uploads_exactly_one_photo_for_all_rows(tmp_path: Path, nas_root: Path) -> None:
    renderer = FakeRenderer()

    state = asyncio.run(_run_batch(tmp_path=tmp_path, rows=_rows(5), renderer=renderer, nas_root=nas_root))

    assert state.done == 5
    assert state.failed_count == 0
    assert len(renderer.uploaded_photos) == 1
    assert renderer.clear_count == 1
    assert renderer.calls.count("submit") == 5
    # Every row rendered against the same shared photo.
    assert {s["photo_id"] for s in renderer.submissions} == {"photo-1"}


def test_each_row_gets_its_own_correlation_id(tmp_path: Path, nas_root: Path) -> None:
    """Renders are tagged per job so a lost submit response can be reconciled
    rather than resubmitted — the duplicate-render guard."""
    renderer = FakeRenderer()

    asyncio.run(_run_batch(tmp_path=tmp_path, rows=_rows(3), renderer=renderer, nas_root=nas_root))

    ids = [s["correlation_id"] for s in renderer.submissions]
    assert len(set(ids)) == 3
    assert all(ids)


def test_rows_fall_back_to_their_own_photo_when_the_shared_upload_fails(
    tmp_path: Path, nas_root: Path
) -> None:
    renderer = FakeRenderer()

    class FailsOnce(TalkingPhotoSlots):
        def __init__(self, renderer):
            super().__init__(renderer)
            self.attempts = 0

        async def acquire(self, **kwargs) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("shared upload failed")
            return await super().acquire(**kwargs)

    state = asyncio.run(
        _run_batch(tmp_path=tmp_path, rows=_rows(3), renderer=renderer, slots=FailsOnce(renderer), nas_root=nas_root)
    )

    assert state.done == 3
    assert len(renderer.uploaded_photos) == 3
    assert len({s["photo_id"] for s in renderer.submissions}) == 3


def test_one_bad_row_does_not_sink_the_batch(tmp_path: Path, nas_root: Path) -> None:
    class FailsSecondRender(FakeRenderer):
        async def await_render(self, *, video_id: str):
            if video_id == "video-2":
                raise RuntimeError("render failed: fake")
            return await super().await_render(video_id=video_id)

    renderer = FailsSecondRender()

    state = asyncio.run(_run_batch(tmp_path=tmp_path, rows=_rows(3), renderer=renderer, nas_root=nas_root))

    assert state.done == 2
    assert state.failed_count == 1
    statuses = {r.row_index: r.status for r in state.rows}
    assert sorted(statuses.values()) == ["completed", "completed", "failed"]
    failed_row = next(r for r in state.rows if r.status == "failed")
    assert "render failed" in (failed_row.error or "")


def test_completed_rows_record_where_the_video_landed(tmp_path: Path, nas_root: Path) -> None:
    renderer = FakeRenderer()

    state = asyncio.run(_run_batch(tmp_path=tmp_path, rows=_rows(2), renderer=renderer, nas_root=nas_root))

    for row in state.rows:
        assert row.status == "completed"
        assert row.nas_path == f"12-08-2026/{row.video_title}.mp4"
        assert (nas_root / "12-08-2026" / f"{row.video_title}.mp4").exists()
