from unittest.mock import patch

from fastapi.testclient import TestClient

from api import routes as api


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ELEVEN_LABS", "test-key")
    return TestClient(api.app)


def test_tts_returns_audio_id_and_uses_speed(monkeypatch) -> None:
    client = _client(monkeypatch)
    with patch("api.routes.synthesize_speech_bytes", return_value=b"xx") as mock:
        resp = client.post(
            "/tts-elevenlabs",
            json={"text": "hello", "voice_id": "v", "speed": 0.9},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["audio_id"].startswith("elevenlabs-")
    assert body["tts_url"].endswith(".mp3")
    cfg = mock.call_args.kwargs["config"]
    assert cfg.model_id == "eleven_multilingual_v2"
    assert cfg.speed == 0.9


def test_tts_rejects_out_of_range_speed(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/tts-elevenlabs",
        json={"text": "hi", "voice_id": "v", "speed": 2.0},
    )
    assert resp.status_code == 422
