"""The header's config dot: does the server's .env have every key the app needs?"""
from fastapi.testclient import TestClient

from api import routes as api
from services.settings import required_keys
from tests.fakes import use_env
from tests.test_api_batch import ENV_TEXT


def _status(monkeypatch, env_text: str) -> dict:
    for key in required_keys():
        monkeypatch.delenv(key, raising=False)
    use_env(monkeypatch, env_text)
    resp = TestClient(api.app).get("/config/session-env/status")
    assert resp.status_code == 200
    return resp.json()


def test_a_complete_env_is_configured(monkeypatch) -> None:
    body = _status(monkeypatch, ENV_TEXT)

    assert body["configured"] is True
    assert body["missing_keys"] == []
    assert "HEYGEN_ISHWARI" in body["required_keys"]


def test_an_env_without_a_heygen_key_is_not_configured(monkeypatch) -> None:
    """The required list used to be maintained by hand and omitted
    HEYGEN_ISHWARI, so an app that could not render a single video reported
    itself as fully configured."""
    env = "\n".join(line for line in ENV_TEXT.splitlines() if not line.startswith("HEYGEN_ISHWARI"))

    body = _status(monkeypatch, env)

    assert body["configured"] is False
    assert body["missing_keys"] == ["HEYGEN_ISHWARI"]
