import time

from fastapi.testclient import TestClient

from api import routes as api


def _poll_job_until_terminal(client: TestClient, job_id: str, retries: int = 30) -> dict:
    for _ in range(retries):
        response = client.get(f"/subtitle/video-jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.01)
    return client.get(f"/subtitle/video-jobs/{job_id}").json()


def test_create_and_get_subtitle_video_job_completed(monkeypatch) -> None:
    async def fake_run_subtitle_video_job(
        *,
        job_id: str,
        input_file_name: str,
        video_path,
        parsed_languages,
        max_chars_per_translation_chunk,
        runtime_config,
        jobs_store,
    ):
        _ = (input_file_name, video_path, max_chars_per_translation_chunk, runtime_config)
        await jobs_store.start(job_id)
        await jobs_store.complete(
            job_id,
            {
                "subtitle_url": "/output/video-source.srt",
                "subtitle_urls": {"source": "/output/video-source.srt"},
                "target_languages": parsed_languages,
            },
        )

    monkeypatch.setattr(api, "run_subtitle_video_job", fake_run_subtitle_video_job)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/video-jobs",
        files={"video": ("clip.mp4", b"fake-video", "video/mp4")},
        data={"target_languages": "hi-IN"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    job_id = payload["job_id"]

    final_state = _poll_job_until_terminal(client, job_id)
    assert final_state["status"] == "completed"
    assert final_state["progress_step"] == "completed"
    assert final_state["progress_percent"] == 100
    assert final_state["result"]["subtitle_url"] == "/output/video-source.srt"
    assert final_state["result"]["target_languages"] == ["hi-IN"]


def test_get_subtitle_video_job_not_found() -> None:
    client = TestClient(api.app)
    response = client.get("/subtitle/video-jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"
