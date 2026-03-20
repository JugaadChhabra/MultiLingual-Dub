from fastapi.testclient import TestClient

from api import routes as api


def _valid_env_text() -> str:
    return "\n".join(
        [
            "ELEVEN_LABS=test-eleven",
            "SARVAM_API=test-sarvam",
            "GEMINI_API_KEY=test-google",
            "WASABI_ENDPOINT_URL=https://s3.ap-southeast-1.wasabisys.com",
            "WASABI_REGION=ap-southeast-1",
            "WASABI_ACCESS_KEY=abc",
            "WASABI_SECRET_KEY=xyz",
            "WASABI_BUCKET=test-bucket",
            "AWS_ACCESS_KEY=abc",
            "AWS_SECRET_KEY=xyz",
            "AWS_BUCKET=test-bucket",
            "AWS_REGION=ap-south-1",
            "BATCH_ENABLE_WASABI_UPLOAD=true",
            "BATCH_ENABLE_QC=true",
            "AI_STUDIO_VOICE=v1",
            "DESI_VOCAL_VOICE=v2",
        ]
    )


def test_session_env_config_lifecycle() -> None:
    client = TestClient(api.app)

    set_resp = client.post("/config/session-env", json={"env_text": _valid_env_text()})
    assert set_resp.status_code == 200
    assert set_resp.json() == {"configured": True, "missing_keys": []}

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
