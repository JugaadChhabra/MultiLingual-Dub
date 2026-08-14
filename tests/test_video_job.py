"""Orchestration tests for the video job.

Everything provider-facing runs through a fake; the NAS runs for real in local
mode against tmp_path, so "did the file land in the right folder" is a real
assertion rather than a recorded call.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.nas import NasConfig
from services.video_pipeline.pipeline import recover_video_job, run_video_job
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec
from tests.fakes import FakeRenderer, FakeSpeech


@pytest.fixture
def nas_root(tmp_path: Path) -> Path:
    """A real NAS in local mode, so 'did the file land in the right folder' is a
    real assertion rather than a recorded call."""
    return tmp_path / "nas"


def _nas(root: Path, *, us_root: Path | None = None) -> NasConfig:
    return NasConfig(
        mode="local",
        root_path=str(root),
        server="",
        share="",
        username="",
        password="",
        domain="",
        port=445,
        us_character_root_path=str(us_root) if us_root else "",
    )


def _spec(**overrides) -> VideoJobSpec:
    base = dict(
        script="hello world",
        character="indian",
        voice_id="voice-abc",
        video_title="My Video",
        publish_date="12-08-2026",
    )
    base.update(overrides)
    return VideoJobSpec(**base)


async def _run(
    *,
    output_dir: Path,
    spec: VideoJobSpec,
    renderer: FakeRenderer,
    speech: FakeSpeech,
    store: VideoJobsStore | None = None,
    job_id: str = "job-1",
    image_bytes: bytes = b"\xff\xd8fake-jpeg",
    nas_config: NasConfig | None = None,
):
    store = store or VideoJobsStore()
    await store.create(job_id)
    await run_video_job(
        job_id=job_id,
        spec=spec,
        image_bytes=image_bytes,
        image_filename="photo.jpg",
        output_dir=output_dir,
        jobs_store=store,
        renderer=renderer,
        speech=speech,
        slots=TalkingPhotoSlots(renderer),
        nas_config=nas_config or _nas(Path("nas")),
    )
    return store, await store.get(job_id)


def test_happy_path_completes_and_lands_on_nas(tmp_path: Path, nas_root: Path) -> None:
    renderer, speech = FakeRenderer(), FakeSpeech()

    _, state = asyncio.run(
        _run(output_dir=tmp_path / "out", spec=_spec(), renderer=renderer, speech=speech, nas_config=_nas(nas_root))
    )

    assert state.status == "completed"
    assert state.error is None
    assert state.summary.heygen_video_id == "video-1"
    assert state.summary.nas_path == "12-08-2026/My Video.mp4"

    landed = nas_root / "12-08-2026" / "My Video.mp4"
    assert landed.read_bytes() == renderer.video_bytes


def test_steps_run_in_order(tmp_path: Path, nas_root: Path) -> None:
    renderer, speech = FakeRenderer(), FakeSpeech()

    asyncio.run(_run(output_dir=tmp_path / "out", spec=_spec(), renderer=renderer, speech=speech, nas_config=_nas(nas_root)))

    assert renderer.calls == [
        "upload_audio",
        "clear_photos",
        "upload_photo",
        "submit",
        "await_render",
        "download",
    ]


def test_slots_are_cleared_before_the_photo_is_uploaded(tmp_path: Path, nas_root: Path) -> None:
    """The rule that stops leftover photos tripping the provider's cap."""
    renderer, speech = FakeRenderer(), FakeSpeech()

    asyncio.run(_run(output_dir=tmp_path / "out", spec=_spec(), renderer=renderer, speech=speech, nas_config=_nas(nas_root)))

    assert renderer.calls.index("clear_photos") < renderer.calls.index("upload_photo")
    assert renderer.clear_count == 1
    assert len(renderer.uploaded_photos) == 1


def test_supplied_photo_id_uploads_nothing(tmp_path: Path, nas_root: Path) -> None:
    """A batch row carries the batch's shared photo id and must not acquire its own."""
    renderer, speech = FakeRenderer(), FakeSpeech()

    _, state = asyncio.run(
        _run(
            output_dir=tmp_path / "out",
            spec=_spec(talking_photo_id="shared-photo"),
            renderer=renderer,
            speech=speech,
            nas_config=_nas(nas_root),
        )
    )

    assert "upload_photo" not in renderer.calls
    assert "clear_photos" not in renderer.calls
    assert renderer.submissions[0]["photo_id"] == "shared-photo"
    assert state.summary.image_key == "shared-photo"


def test_a_clear_failure_does_not_stop_the_upload(tmp_path: Path, nas_root: Path) -> None:
    class ClearFails(FakeRenderer):
        async def clear_photos(self) -> int:
            self.calls.append("clear_photos")
            raise RuntimeError("provider refused")

    renderer, speech = ClearFails(), FakeSpeech()

    _, state = asyncio.run(
        _run(output_dir=tmp_path / "out", spec=_spec(), renderer=renderer, speech=speech, nas_config=_nas(nas_root))
    )

    assert state.status == "completed"
    assert len(renderer.uploaded_photos) == 1


def test_repeat_script_reuses_cached_audio(tmp_path: Path, nas_root: Path) -> None:
    """Content-hash cache: the second job must not re-bill the provider."""
    output_dir = tmp_path / "out"
    speech = FakeSpeech()

    asyncio.run(_run(output_dir=output_dir, spec=_spec(), renderer=FakeRenderer(), speech=speech, job_id="job-1", nas_config=_nas(nas_root)))
    assert speech.call_count == 1

    _, state = asyncio.run(
        _run(output_dir=output_dir, spec=_spec(), renderer=FakeRenderer(), speech=speech, job_id="job-2", nas_config=_nas(nas_root))
    )

    assert speech.call_count == 1
    assert state.status == "completed"
    assert state.summary.audio_bytes == len(speech.audio)


def test_changing_a_voice_setting_busts_the_cache(tmp_path: Path, nas_root: Path) -> None:
    output_dir = tmp_path / "out"
    speech = FakeSpeech()

    asyncio.run(_run(output_dir=output_dir, spec=_spec(), renderer=FakeRenderer(), speech=speech, job_id="job-1", nas_config=_nas(nas_root)))
    asyncio.run(
        _run(
            output_dir=output_dir,
            spec=_spec(stability=0.9),
            renderer=FakeRenderer(),
            speech=speech,
            job_id="job-2",
            nas_config=_nas(nas_root),
        )
    )

    assert speech.call_count == 2


def test_us_character_lands_in_its_own_nas_root(tmp_path: Path, nas_root: Path) -> None:
    us_root = tmp_path / "nas-us"
    renderer, speech = FakeRenderer(), FakeSpeech()

    _, state = asyncio.run(
        _run(
            output_dir=tmp_path / "out",
            spec=_spec(character="us"),
            renderer=renderer,
            speech=speech,
            nas_config=_nas(nas_root, us_root=us_root),
        )
    )

    assert state.status == "completed"
    assert (us_root / "12-08-2026" / "My Video.mp4").exists()
    assert not (nas_root / "12-08-2026" / "My Video.mp4").exists()


def test_missing_voice_fails_the_job(tmp_path: Path, nas_root: Path) -> None:
    """No voice on the spec and none configured for the character."""
    renderer, speech = FakeRenderer(), FakeSpeech()

    _, state = asyncio.run(
        _run(
            output_dir=tmp_path / "out",
            spec=_spec(voice_id=None),
            renderer=renderer,
            speech=speech,
            nas_config=_nas(nas_root),
        )
    )

    assert state.status == "failed"
    assert "voice_id" in (state.error or "")
    assert renderer.calls == []


def test_render_failure_fails_the_job(tmp_path: Path, nas_root: Path) -> None:
    renderer, speech = FakeRenderer(render_failed=True), FakeSpeech()

    _, state = asyncio.run(
        _run(output_dir=tmp_path / "out", spec=_spec(), renderer=renderer, speech=speech, nas_config=_nas(nas_root))
    )

    assert state.status == "failed"
    assert "render failed" in (state.error or "")


def test_download_failure_leaves_the_job_recoverable(tmp_path: Path, nas_root: Path) -> None:
    """The render is finished and paid for — losing its id would waste it."""
    renderer, speech = FakeRenderer(fail_download=True), FakeSpeech()

    store, state = asyncio.run(
        _run(output_dir=tmp_path / "out", spec=_spec(), renderer=renderer, speech=speech, nas_config=_nas(nas_root))
    )

    assert state.status == "failed"
    assert state.summary.heygen_video_id == "video-1"
    assert state.spec is not None
    assert asyncio.run(store.list_recoverable()) == ["job-1"]


def test_recovery_re_fetches_and_completes(tmp_path: Path, nas_root: Path) -> None:
    output_dir = tmp_path / "out"
    speech = FakeSpeech()

    store, failed = asyncio.run(
        _run(
            output_dir=output_dir,
            spec=_spec(),
            renderer=FakeRenderer(fail_download=True),
            speech=speech,
            nas_config=_nas(nas_root),
        )
    )
    assert failed.status == "failed"

    healthy = FakeRenderer()
    asyncio.run(
        recover_video_job(
            job_id="job-1",
            jobs_store=store,
            output_dir=output_dir,
            renderer=healthy,
            nas_config=_nas(nas_root),
        )
    )

    state = asyncio.run(store.get("job-1"))
    assert state.status == "completed"
    assert state.summary.nas_path == "12-08-2026/My Video.mp4"
    assert (nas_root / "12-08-2026" / "My Video.mp4").exists()
    # Recovery re-enters at the render, so it never re-runs TTS or re-submits.
    assert healthy.calls == ["await_render", "download"]
    assert speech.call_count == 1


def test_recovery_refuses_a_job_with_no_render(tmp_path: Path) -> None:
    async def _go():
        store = VideoJobsStore()
        await store.create("job-x")
        await recover_video_job(
            job_id="job-x",
            jobs_store=store,
            output_dir=tmp_path / "out",
            renderer=FakeRenderer(),
            nas_config=_nas(tmp_path / "nas"),
        )

    with pytest.raises(ValueError, match="no render id"):
        asyncio.run(_go())


def test_persisted_jobs_survive_a_restart(tmp_path: Path, nas_root: Path) -> None:
    """A new store over the same directory reloads what the old one wrote."""
    persist = tmp_path / "jobs"

    async def _go():
        store = VideoJobsStore(persist_dir=persist)
        await store.create("job-1")
        await run_video_job(
            job_id="job-1",
            spec=_spec(),
            image_bytes=b"\xff\xd8fake-jpeg",
            image_filename="photo.jpg",
            output_dir=tmp_path / "out",
            jobs_store=store,
            renderer=FakeRenderer(fail_download=True),
            speech=FakeSpeech(),
            slots=TalkingPhotoSlots(FakeRenderer()),
            nas_config=_nas(nas_root),
        )

    asyncio.run(_go())

    reloaded = VideoJobsStore(persist_dir=persist)
    assert asyncio.run(reloaded.list_recoverable()) == ["job-1"]
    state = asyncio.run(reloaded.get("job-1"))
    assert state.spec is not None
    assert state.spec.video_title == "My Video"
