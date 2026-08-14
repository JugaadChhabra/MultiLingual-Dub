from fastapi.testclient import TestClient

from api import routes as api


def _valid_env_text() -> str:
    return "\n".join(
        [
            "ELEVEN_LABS=test-eleven",
            "SARVAM_API=test-sarvam",
            "GEMINI_API_KEY=test-google",
            "AWS_ACCESS_KEY=abc",
            "AWS_SECRET_KEY=xyz",
            "AWS_BUCKET=test-bucket",
            "AWS_REGION=ap-south-1",
            "BATCH_ENABLE_QC=true",
            "HEYGEN_ISHWARI=test-heygen",
            "DESI_VOCAL_VOICE=v2",
            "ENGLISH_VOICE=v3",
        ]
    )


def test_session_env_config_lifecycle() -> None:
    client = TestClient(api.app)

    set_resp = client.post("/config/session-env", json={"env_text": _valid_env_text()})
    assert set_resp.status_code == 200
    body = set_resp.json()
    assert body["configured"] is True
    assert body["missing_keys"] == []
    assert "HEYGEN_ISHWARI" in body["required_keys"]

    status_resp = client.get("/config/session-env/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["configured"] is True

    clear_resp = client.delete("/config/session-env")
    assert clear_resp.status_code == 200
    assert clear_resp.json()["configured"] is False


def test_session_env_config_rejects_missing_required_keys() -> None:
    client = TestClient(api.app)
    resp = client.post("/config/session-env", json={"env_text": "ELEVEN_LABS=abc"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "missing_keys" in detail
    assert "SARVAM_API" in detail["missing_keys"]


def test_a_config_without_a_heygen_key_is_not_reported_as_configured() -> None:
    """The required list used to be maintained by hand and omitted
    HEYGEN_ISHWARI, so a session that could not render a single video reported
    itself as fully configured."""
    client = TestClient(api.app)
    env_text = _valid_env_text().replace("HEYGEN_ISHWARI=test-heygen\n", "")

    resp = client.post("/config/session-env", json={"env_text": env_text})

    assert resp.status_code == 400
    assert "HEYGEN_ISHWARI" in resp.json()["detail"]["missing_keys"]
