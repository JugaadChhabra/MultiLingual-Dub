from unittest.mock import patch

from fastapi.testclient import TestClient

from api import routes as api


def test_from_audio_rejects_bad_audio_id() -> None:
    client = TestClient(api.app)
    resp = client.post(
        "/video/heygen/from-audio",
        data={"audio_id": "does-not-exist", "talking_photo_id": "tp_1"},
    )
    assert resp.status_code == 400


def test_from_audio_queues_job(tmp_path, monkeypatch) -> None:
    # audio files resolve under OUTPUT_DIR; point it at tmp and drop a file
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path)
    (tmp_path / "elevenlabs-abc.mp3").write_bytes(b"xx")
    client = TestClient(api.app)
    with patch("api.routes.run_video_job", return_value=None), \
         patch("api.routes.asyncio.create_task") as mock_task:
        resp = client.post(
            "/video/heygen/from-audio",
            data={"audio_id": "elevenlabs-abc", "talking_photo_id": "tp_1"},
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert mock_task.called
