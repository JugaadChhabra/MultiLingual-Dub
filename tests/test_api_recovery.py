"""Route tests for Recovery: list recoverable, recover one, recover all failed.

The renderer is a FakeRenderer and the NAS is a local folder under tmp_path.
``spawn`` is swapped for a recorder, so each test runs the recoveries it
launched to completion itself — and can also hold one back to see how a second
request is treated while the first is still in flight.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import routes as api
from services.nas import NasConfig
from services.runtime_config import MissingSettingError
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.speech import SpeechSynth
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec
from tests.fakes import FakeRenderer, FakeSpeech


@pytest.fixture
def store(tmp_path, monkeypatch) -> VideoJobsStore:
    store = VideoJobsStore(persist_dir=tmp_path / "jobs")
    monkeypatch.setattr(api, "video_jobs_store", store)
    monkeypatch.setattr(api, "VIDEO_OUTPUT_DIR", tmp_path / "out")
    return store


@pytest.fixture
def renderer(tmp_path, monkeypatch) -> FakeRenderer:
    renderer = FakeRenderer()
    nas = NasConfig(
        mode="local", root_path=str(tmp_path / "nas"),
        server="", share="", username="", password="", domain="", port=445,
    )

    def build(session):
        return api.VideoDeps(
            renderer=renderer,
            speech=FakeSpeech(),
            slots=TalkingPhotoSlots(renderer),
            heygen=None,
            nas=nas,
            email=None,
        )

    monkeypatch.setattr(api, "_build_video_deps", build)
    return renderer


@pytest.fixture
def launched(monkeypatch):
    """Coroutines the routes handed to spawn, in order, not yet run."""
    calls: list = []

    def fake_spawn(coro, *, name):
        calls.append((name, coro))

    monkeypatch.setattr(api, "spawn", fake_spawn)
    yield calls
    for _, coro in calls:
        coro.close()  # any a test held back; a no-op for those it ran


@pytest.fixture
def client(store, renderer, launched) -> TestClient:
    return TestClient(api.app)


def _spec() -> VideoJobSpec:
    return VideoJobSpec(
        script="hello", video_title="My Video", publish_date="12-08-2026",
        include_overlay=False, include_music=False,
    )


def _job(store: VideoJobsStore, job_id: str, *, status: str = "failed",
         video_id: str | None = "video-9", spec: bool = True) -> None:
    async def _go():
        await store.create(job_id)
        if spec:
            await store.set_spec(job_id, _spec())
        if video_id:
            await store.patch_summary(job_id, heygen_video_id=video_id)
        if status == "failed":
            await store.fail(job_id, "download failed")
        elif status == "completed":
            await store.complete(job_id)
        else:
            await store.set_status(job_id, status)

    asyncio.run(_go())


def _run(launched: list) -> None:
    for _, coro in launched:
        asyncio.run(coro)


def _status(store: VideoJobsStore, job_id: str) -> str:
    return asyncio.run(store.get(job_id)).status


# --- list ------------------------------------------------------------------


def test_the_list_offers_only_failed_jobs_with_a_render_and_spec(client, store) -> None:
    _job(store, "a")
    _job(store, "b", video_id=None)
    _job(store, "c", status="completed")
    _job(store, "d", spec=False)

    resp = client.get("/video/heygen/jobs/recoverable")

    assert resp.status_code == 200
    assert resp.json() == {"recoverable": ["a"], "count": 1}


# --- recover one -----------------------------------------------------------


def test_recovering_a_failed_job_files_it_to_the_nas(client, store, renderer, launched, tmp_path) -> None:
    _job(store, "a")

    resp = client.post("/video/heygen/a/recover")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"job_id": "a", "status": "recovering"}
    assert _status(store, "a") == "recovering"
    _run(launched)

    state = asyncio.run(store.get("a"))
    assert state.status == "completed"
    assert state.summary.nas_path == "12-08-2026/My Video.mp4"
    assert (tmp_path / "nas" / "12-08-2026" / "My Video.mp4").exists()
    assert renderer.calls == ["await_render", "download"]


def test_an_unknown_job_is_404(client, launched) -> None:
    resp = client.post("/video/heygen/nope/recover")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Job not found"
    assert launched == []


@pytest.mark.parametrize(
    "job, detail",
    [
        (dict(video_id=None), "Job has no HeyGen render to recover"),
        (dict(spec=False), "Job has no persisted spec to recover from"),
        (dict(status="completed"), "Job has not failed (status: completed); only a failed job can be recovered"),
        (dict(status="polling"), "Job has not failed (status: polling); only a failed job can be recovered"),
    ],
)
def test_an_unrecoverable_job_is_409(client, store, launched, job, detail) -> None:
    _job(store, "a", **job)
    before = _status(store, "a")

    resp = client.post("/video/heygen/a/recover")

    assert resp.status_code == 409
    assert resp.json()["detail"] == detail
    assert launched == []
    assert _status(store, "a") == before


def test_a_second_request_while_recovering_is_409(client, store, launched) -> None:
    _job(store, "a")

    first = client.post("/video/heygen/a/recover")
    second = client.post("/video/heygen/a/recover")

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["detail"] == "Job is already being recovered"
    assert len(launched) == 1


def test_missing_settings_are_400_and_leave_the_job_alone(client, store, launched, monkeypatch) -> None:
    _job(store, "a")

    def build(session):
        raise MissingSettingError(["HEYGEN_ISHWARI"])

    monkeypatch.setattr(api, "_build_video_deps", build)

    one = client.post("/video/heygen/a/recover")
    every = client.post("/video/heygen/recover-failed")

    assert (one.status_code, every.status_code) == (400, 400)
    assert "HEYGEN_ISHWARI" in one.json()["detail"]
    assert launched == []
    assert _status(store, "a") == "failed"


def test_a_failed_recovery_leaves_the_job_recoverable(client, store, renderer, launched) -> None:
    _job(store, "a")
    renderer.fail_download = True

    client.post("/video/heygen/a/recover")
    with pytest.raises(RuntimeError):
        _run(launched)

    assert _status(store, "a") == "failed"
    assert client.get("/video/heygen/jobs/recoverable").json()["recoverable"] == ["a"]


def test_a_render_heygen_failed_is_not_recoverable(client, store, renderer, launched) -> None:
    """Recovery re-fetches a render; if HeyGen failed it there is nothing to
    fetch. It used to stay on offer and fail every time it was tried."""
    _job(store, "a")
    renderer.render_failed = True

    client.post("/video/heygen/a/recover")
    with pytest.raises(RuntimeError):
        _run(launched)

    state = asyncio.run(store.get("a"))
    assert state.status == "failed"
    assert state.summary.render_failed is True
    assert client.get("/video/heygen/jobs/recoverable").json()["count"] == 0
    again = client.post("/video/heygen/a/recover")
    assert again.status_code == 409
    assert "HeyGen's render failed" in again.json()["detail"]


def test_a_job_whose_render_failed_first_time_is_never_offered(store, renderer, tmp_path) -> None:
    from services.video_pipeline import run_video_job
    from services.video_pipeline.slots import TalkingPhotoSlots
    renderer.render_failed = True

    async def _go():
        await store.create("a")
        await run_video_job(
            job_id="a", spec=_spec(), image_bytes=b"img", image_filename="i.jpg",
            output_dir=tmp_path / "out", jobs_store=store, renderer=renderer,
            speech=FakeSpeech(), slots=TalkingPhotoSlots(renderer),
            nas_config=None, voice_id="v",
        )
        return await store.get("a")

    state = asyncio.run(_go())

    assert state.status == "failed"
    assert state.summary.render_failed is True
    assert asyncio.run(store.list_recoverable()) == []


# --- recover all -----------------------------------------------------------


def test_recover_all_recovers_exactly_the_failed_jobs_with_a_render(client, store, launched) -> None:
    _job(store, "failed-1")
    _job(store, "failed-2", video_id="video-2")
    _job(store, "no-render", video_id=None)
    _job(store, "no-spec", spec=False)
    _job(store, "done", status="completed")
    _job(store, "running", status="polling")
    _job(store, "busy")
    client.post("/video/heygen/busy/recover")
    assert len(launched) == 1

    resp = client.post("/video/heygen/recover-failed")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "recovering"
    assert sorted(body["job_ids"]) == ["failed-1", "failed-2"]
    assert sorted(body["video_ids"]) == ["video-2", "video-9"]
    assert body["count"] == 2
    assert sorted(name for name, _ in launched[1:]) == ["video-recover:failed-1", "video-recover:failed-2"]
    _run(launched)
    for jid in ("failed-1", "failed-2", "busy"):
        assert _status(store, jid) == "completed"
    assert _status(store, "no-render") == "failed"
    assert _status(store, "no-spec") == "failed"


def test_a_recovery_that_cannot_be_launched_releases_its_claim(client, store, monkeypatch) -> None:
    _job(store, "a")

    def broken_spawn(coro, *, name):
        raise RuntimeError("no event loop")

    monkeypatch.setattr(api, "spawn", broken_spawn)

    with pytest.raises(RuntimeError):
        client.post("/video/heygen/a/recover")

    assert _status(store, "a") == "failed"
    assert asyncio.run(store.list_recoverable()) == ["a"]
