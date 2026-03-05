from io import BytesIO
import time

from fastapi.testclient import TestClient
from openpyxl import Workbook

from api import routes as api


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["text", "emotion", "activity_name", "audio_type"])
    ws.append(["hello", "happy", "activity", "promo"])
    buff = BytesIO()
    wb.save(buff)
    return buff.getvalue()


async def _fake_job_runner(*, job_id, excel_path, target_languages, jobs_store):
    """Shared stub: immediately completes a job with 1 row and N language tasks."""
    await jobs_store.start(job_id)
    summary = (await jobs_store.get(job_id)).summary
    summary.total_rows = 1
    summary.rows_processed = 1
    summary.rows_succeeded = 1
    summary.language_tasks_total = len(target_languages)
    summary.language_tasks_succeeded = len(target_languages)
    summary.uploads_succeeded = len(target_languages)
    await jobs_store.complete(job_id, summary)


def _poll_until_done(client: TestClient, job_id: str, retries: int = 20) -> dict:
    """Poll GET /batch/excel-jobs/{job_id} until status is terminal or retries exhausted."""
    for _ in range(retries):
        resp = client.get(f"/batch/excel-jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in {"completed", "failed"}:
            return data
        time.sleep(0.01)
    return client.get(f"/batch/excel-jobs/{job_id}").json()


def test_create_excel_job_rejects_non_xlsx() -> None:
    client = TestClient(api.app)
    response = client.post(
        "/batch/excel-jobs",
        files={"file": ("bad.txt", b"hello", "text/plain")},
        data={"target_languages": "hi-IN"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only .xlsx files are allowed"


def test_create_and_get_excel_job(monkeypatch) -> None:
    monkeypatch.setattr(api, "run_excel_batch_job", _fake_job_runner)

    client = TestClient(api.app)
    response = client.post(
        "/batch/excel-jobs",
        files={
            "file": (
                "input.xlsx",
                _xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"target_languages_json": '["hi-IN", "ta-IN"]'},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    final = _poll_until_done(client, job_id)
    assert final["status"] == "completed"
    assert final["summary"]["rows_processed"] == 1
    assert final["summary"]["language_tasks_total"] == 2


def test_create_excel_job_deduplicates_repeated_target_languages(monkeypatch) -> None:
    monkeypatch.setattr(api, "run_excel_batch_job", _fake_job_runner)

    client = TestClient(api.app)
    response = client.post(
        "/batch/excel-jobs",
        files=[
            (
                "file",
                (
                    "input.xlsx",
                    _xlsx_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
            ("target_languages", (None, "hi-IN")),
            ("target_languages", (None, "hi-IN")),  # duplicate — should be collapsed to 1
            ("target_languages", (None, "ta-IN")),
        ],
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    final = _poll_until_done(client, job_id)
    assert final["status"] == "completed"
    assert final["summary"]["rows_processed"] == 1
    assert final["summary"]["language_tasks_total"] == 2


def test_get_excel_job_not_found() -> None:
    client = TestClient(api.app)
    response = client.get("/batch/excel-jobs/does-not-exist")
    assert response.status_code == 404
