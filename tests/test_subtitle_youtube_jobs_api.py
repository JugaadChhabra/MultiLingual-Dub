import time

from fastapi.testclient import TestClient

from api import routes as api


def _poll_job_until_terminal(client: TestClient, job_id: str, retries: int = 30) -> dict:
    for _ in range(retries):
        response = client.get(f"/subtitle/youtube-jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.01)
    return client.get(f"/subtitle/youtube-jobs/{job_id}").json()


def test_create_and_get_subtitle_youtube_job_completed(monkeypatch) -> None:
    async def fake_run_subtitle_youtube_job(
        *,
        job_id: str,
        payload,
        parsed_languages,
        runtime_config,
        jobs_store,
    ) -> None:
        _ = (payload, runtime_config)
        await jobs_store.start(job_id)
        await jobs_store.complete(
            job_id,
            {
                "subtitle_url": "/output/fake.srt",
                "subtitle_urls": {"source": "/output/fake.srt"},
                "target_languages": parsed_languages,
            },
        )

    monkeypatch.setattr(api, "run_subtitle_youtube_job", fake_run_subtitle_youtube_job)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/youtube-jobs",
        json={
            "youtube_url": "https://www.youtube.com/watch?v=abc123",
            "target_languages": ["hi-IN", "hi-IN", "ta-IN"],
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    job_id = payload["job_id"]

    final_state = _poll_job_until_terminal(client, job_id)
    assert final_state["status"] == "completed"
    assert final_state["progress_step"] == "completed"
    assert final_state["progress_percent"] == 100
    assert final_state["result"]["subtitle_url"] == "/output/fake.srt"
    assert final_state["result"]["target_languages"] == ["hi-IN", "ta-IN"]


def test_get_subtitle_youtube_job_not_found() -> None:
    client = TestClient(api.app)
    response = client.get("/subtitle/youtube-jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_create_and_get_subtitle_youtube_job_failed(monkeypatch) -> None:
    async def fake_run_subtitle_youtube_job(
        *,
        job_id: str,
        payload,
        parsed_languages,
        runtime_config,
        jobs_store,
    ) -> None:
        _ = (payload, parsed_languages, runtime_config)
        await jobs_store.start(job_id)
        await jobs_store.fail(job_id, "Simulated pipeline failure")

    monkeypatch.setattr(api, "run_subtitle_youtube_job", fake_run_subtitle_youtube_job)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/youtube-jobs",
        json={"youtube_url": "https://www.youtube.com/watch?v=xyz987"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    final_state = _poll_job_until_terminal(client, job_id)
    assert final_state["status"] == "failed"
    assert final_state["progress_message"] == "Simulated pipeline failure"
    assert final_state["error"] == "Simulated pipeline failure"
