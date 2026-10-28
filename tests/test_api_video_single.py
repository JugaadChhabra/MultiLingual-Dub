"""Route tests for the single-video request: it hands spec building to intake.

The job launcher is swapped out, so nothing is synthesised, rendered or filed —
the tests read the spec the job would have run with.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import routes as api
from api import shared
from api.routers import video as video_routes
from services.script_writer import MAX_SCRIPT_CHARS
from services.video_pipeline.store import VideoJobsStore
from tests.fakes import use_env
from tests.test_api_script_generation import ENV_TEXT


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # Jobs persist to disk; keep them out of the real output/ folder.
    monkeypatch.setattr(shared, "video_jobs_store", VideoJobsStore(persist_dir=tmp_path / "jobs"))
    client = TestClient(api.app)
    use_env(monkeypatch, ENV_TEXT)
    return client


@pytest.fixture
def launched(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_job(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(video_routes, "run_video_job", fake_job)
    return calls


def _image():
    return {"image": ("avatar.jpg", b"jpeg-bytes", "image/jpeg")}


def test_a_submission_reaches_the_job_as_a_validated_spec(client, launched) -> None:
    resp = client.post(
        "/video/heygen",
        files=_image(),
        data={
            "script": "  मेष का राशिफल  ",
            "video_title": "Aries: 14/08.mp4",
            "character": "",
            "voice_id": "",
            "publish_date": "2026-08-14",
            "include_overlay": "false",
            "include_music": "true",
        },
    )

    assert resp.status_code == 202, resp.text
    assert len(launched) == 1
    job = launched[0]
    assert job["job_id"] == resp.json()["job_id"]
    assert job["image_bytes"] == b"jpeg-bytes"
    spec = job["spec"]
    assert spec.script == "मेष का राशिफल"
    assert spec.video_title == "Aries_ 14_08"
    assert spec.character == "indian"
    assert spec.voice_id is None
    assert spec.publish_date == "2026-08-14"
    assert (spec.include_overlay, spec.include_music) == (False, True)


def test_a_script_over_the_writers_limit_is_rejected(client, launched) -> None:
    resp = client.post(
        "/video/heygen",
        files=_image(),
        data={"script": "x" * (MAX_SCRIPT_CHARS + 1), "video_title": "Aries"},
    )

    assert resp.status_code == 400
    assert str(MAX_SCRIPT_CHARS) in resp.json()["detail"]
    assert launched == []


def test_a_whitespace_talking_photo_id_with_no_image_is_refused(client, launched) -> None:
    resp = client.post(
        "/video/heygen",
        data={"script": "hello", "video_title": "Aries", "talking_photo_id": "   "},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "provide either an image file or a talking_photo_id"
    assert launched == []
