"""Pause and cancel for video jobs and batches.

The renders here are held open on an event, so a test can act on a job while it
is mid-render — the moment an operator actually reaches for Cancel.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from services.email import EmailSettings
from services.nas import NasConfig
from services.video_pipeline.batch_excel import HeyGenBatchRow
from services.video_pipeline.batch_runner import run_video_batch_job
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.control import JobControl
from services.video_pipeline.heygen_client import HeyGenSettings
from services.video_pipeline.intake import BatchSettings, specs_from_batch
from services.video_pipeline.library import CANCELLED, build_library
from services.video_pipeline.pipeline import run_video_job
from services.video_pipeline.recovery import is_recoverable
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec
from tests.fakes import FakeRenderer, FakeSpeech


class HeldRenderer(FakeRenderer):
    """A render that does not finish until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.rendering = asyncio.Event()
        self.release = asyncio.Event()

    async def await_render(self, *, video_id: str):
        self.rendering.set()
        await self.release.wait()
        return await super().await_render(video_id=video_id)


def _nas(root: Path) -> NasConfig:
    return NasConfig(mode="local", root_path=str(root), server="", share="",
                     username="", password="", domain="", port=445)


def _spec() -> VideoJobSpec:
    return VideoJobSpec(script="hello", voice_id="voice-abc", video_title="My Video",
                        publish_date="12-08-2026", include_overlay=False, include_music=False)


def _nas_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_cancelling_mid_render_ends_the_job_cancelled_and_files_nothing(tmp_path: Path) -> None:
    async def scenario():
        renderer, store, control = HeldRenderer(), VideoJobsStore(), JobControl()
        await store.create("job-1")
        run = asyncio.ensure_future(run_video_job(
            job_id="job-1", spec=_spec(), image_bytes=b"\xff\xd8fake", image_filename="p.jpg",
            output_dir=tmp_path / "out", jobs_store=store, renderer=renderer, speech=FakeSpeech(),
            slots=TalkingPhotoSlots(renderer), nas_config=_nas(tmp_path / "nas"), control=control,
        ))
        await renderer.rendering.wait()
        control.cancel()
        await asyncio.wait_for(run, 2)
        return renderer, await store.get("job-1")

    renderer, state = asyncio.run(scenario())

    assert state.status == "cancelled"
    assert "download" not in renderer.calls
    assert _nas_files(tmp_path / "nas") == []
    # Cancelled is not failed: Recovery must not offer to file it.
    assert not is_recoverable(state)


def test_cancelled_before_submission_never_reaches_heygen(tmp_path: Path) -> None:
    async def scenario():
        renderer, store, control = FakeRenderer(), VideoJobsStore(), JobControl()
        await store.create("job-1")
        control.cancel()
        await run_video_job(
            job_id="job-1", spec=_spec(), image_bytes=b"\xff\xd8fake", image_filename="p.jpg",
            output_dir=tmp_path / "out", jobs_store=store, renderer=renderer, speech=FakeSpeech(),
            slots=TalkingPhotoSlots(renderer), nas_config=_nas(tmp_path / "nas"), control=control,
        )
        return renderer, await store.get("job-1")

    renderer, state = asyncio.run(scenario())

    assert state.status == "cancelled"
    assert "submit" not in renderer.calls


async def _start_batch(tmp_path: Path, renderer: FakeRenderer, control: JobControl, n: int):
    rows = specs_from_batch(
        [HeyGenBatchRow(row_index=i, script=f"script {i}", video_title=f"Title {i}") for i in range(2, 2 + n)],
        BatchSettings(character="indian", publish_date="12-08-2026",
                      include_overlay="false", include_music="false"),
    )
    store = VideoBatchJobsStore()
    await store.create("b", [BatchRowState(row_index=r.row_index, script=r.spec.script,
                                            video_title=r.spec.video_title) for r in rows])
    run = asyncio.ensure_future(run_video_batch_job(
        batch_id="b", items=rows, image_bytes=b"\xff\xd8fake", image_filename="p.jpg",
        output_dir=tmp_path / "out" / "heygen", output_base_dir=tmp_path / "out",
        batch_store=store, video_jobs_store=VideoJobsStore(), renderer=renderer,
        speech=FakeSpeech(), slots=TalkingPhotoSlots(renderer),
        heygen=HeyGenSettings(api_key="k", character_voice_ids={"indian": "v"}, batch_concurrency=1),
        nas_config=_nas(tmp_path / "nas"),
        email=EmailSettings(api_key="", from_address="", to_addresses=()),
        control=control,
    ))
    return store, run


def test_a_paused_batch_starts_no_new_row_until_resumed(tmp_path: Path) -> None:
    async def scenario():
        renderer, control = HeldRenderer(), JobControl()
        store, run = await _start_batch(tmp_path, renderer, control, 3)
        await renderer.rendering.wait()
        control.pause()
        renderer.release.set()   # the row HeyGen already has finishes anyway
        for _ in range(50):
            await asyncio.sleep(0)
        held = await store.get("b")
        control.resume()
        await asyncio.wait_for(run, 2)
        return renderer, held, await store.get("b")

    renderer, held, final = asyncio.run(scenario())

    assert [r.status for r in held.rows] == ["completed", "pending", "pending"]
    assert final.status == "completed" and final.done == 3
    assert renderer.calls.count("submit") == 3


def test_cancelling_a_batch_stops_every_unfinished_row(tmp_path: Path) -> None:
    async def scenario():
        renderer, control = HeldRenderer(), JobControl()
        store, run = await _start_batch(tmp_path, renderer, control, 3)
        await renderer.rendering.wait()
        control.cancel()
        await asyncio.wait_for(run, 2)
        return renderer, await store.get("b")

    renderer, state = asyncio.run(scenario())

    assert state.status == "cancelled"
    assert [r.status for r in state.rows] == ["cancelled"] * 3
    assert renderer.calls.count("submit") == 1
    assert _nas_files(tmp_path / "nas") == []


def test_library_says_a_cancelled_render_was_not_filed_and_hides_one_never_sent() -> None:
    async def scenario():
        store = VideoJobsStore()
        for job_id in ("sent", "unsent"):
            await store.create(job_id)
            await store.set_spec(job_id, _spec())
        await store.patch_summary("sent", heygen_video_id="video-1")
        await store.cancel("sent")
        await store.cancel("unsent")
        return await store.snapshot()

    entries = build_library([], asyncio.run(scenario()))

    assert [(e.job_id, e.state, e.reason) for e in entries] == [("sent", "cancelled", CANCELLED)]
