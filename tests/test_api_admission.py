"""Admission at the HTTP seam: oversized uploads, overlong sheets and a full
job queue are turned away before any job exists, and leave no temp file.

The queue tests reuse the blocking runners of tests/test_api_job_queue.py, so
"one running, one waiting" holds until the test releases them.
"""
from __future__ import annotations

import tempfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from api import routes as api
from api import shared
from api.routers import audio as audio_routes
from api.routers import story as story_routes
from batch.job_queue import JobQueue
from batch.store import JobsStore
from tests.fakes import use_env
from tests.test_api_batch import ENV_TEXT
from tests.test_api_job_queue import (
    _settle,
    _submit_excel,
    _submit_story,
    _xlsx,
    env,  # noqa: F401  (fixture)
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path) -> None:
    use_env(monkeypatch, ENV_TEXT)
    # Every temp file the app makes lands here, so the tests can see leftovers.
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))


def _temp_files() -> list[str]:
    return sorted(p.name for p in Path(tempfile.tempdir).iterdir())


def _sheet(rows: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["voiceover_text", "emotion", "activity_name", "voiceover_title"])
    for n in range(rows):
        ws.append([f"line {n}", "happy", "activity", f"title{n}"])
    buff = BytesIO()
    wb.save(buff)
    return buff.getvalue()


@pytest.fixture
def client(monkeypatch):
    """A client whose runner would record any job that got through."""
    store = JobsStore()
    monkeypatch.setattr(shared, "jobs_store", store)
    monkeypatch.setattr(shared, "job_queue", JobQueue(store))
    ran: list[str] = []

    async def runner(*, job_id, **_kwargs):
        ran.append(job_id)

    monkeypatch.setattr(audio_routes, "run_excel_batch_job", runner)
    c = TestClient(api.app)
    c.ran = ran
    return c


def _post(client, payload: bytes):
    return client.post(
        "/batch/excel-jobs",
        files={"file": ("input.xlsx", payload, XLSX)},
        data={"target_languages": "hi-IN"},
    )


def _active_ids(client) -> set[str]:
    """Every queued or running job. A job the fake runner got would stay
    ``queued`` (it never starts it), so a created job always shows here."""
    r = client.get("/batch/active")
    assert r.status_code == 200
    return {j["job_id"] for j in r.json()}


def test_an_upload_over_the_size_cap_gets_413_and_creates_no_job(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "0.01")
    payload = _sheet(1) + b"\0" * 20_000  # past 0.01 MB (~10 KB)

    r = _post(client, payload)

    assert r.status_code == 413
    assert "0.01 MB" in r.json()["detail"]
    assert _active_ids(client) == set()
    assert client.ran == []
    assert _temp_files() == []


def test_preview_is_capped_too(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "0.01")
    r = client.post("/batch/preview-excel",
                    files={"file": ("input.xlsx", b"\0" * 20_000, XLSX)})
    assert r.status_code == 413
    assert _temp_files() == []


def test_a_sheet_over_the_row_cap_gets_400_naming_both_numbers(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SHEET_ROWS", "3")

    r = _post(client, _sheet(5))

    assert r.status_code == 400
    assert r.json()["detail"] == (
        "This sheet has 5 rows; the limit is 3. Split it into smaller sheets."
    )
    assert _active_ids(client) == set()
    assert client.ran == []
    assert _temp_files() == []


def test_large_counts_are_written_with_separators(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SHEET_ROWS", "1000")
    r = _post(client, _sheet(1001))
    assert r.json()["detail"].startswith("This sheet has 1,001 rows; the limit is 1,000.")


def test_a_sheet_at_the_row_cap_is_accepted(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SHEET_ROWS", "3")
    r = _post(client, _sheet(3))
    assert r.status_code == 202, r.text


@pytest.fixture
def full_queue(env, monkeypatch):  # noqa: F811
    """One job running and one waiting, with room for exactly one waiter."""
    client, gate = env
    monkeypatch.setenv("AUDIO_MAX_CONCURRENT_JOBS", "1")
    monkeypatch.setenv("MAX_QUEUED_JOBS", "1")
    monkeypatch.setattr(shared, "job_queue", JobQueue(shared.jobs_store))
    ids = [_submit_excel(client) for _ in range(2)]
    _settle(client, ids, [("running", None), ("queued", 1)])
    return client, gate, ids


def test_an_excel_job_beyond_the_waiting_cap_gets_503_with_retry_after(full_queue) -> None:
    client, _gate, ids = full_queue
    before = _active_ids(client)

    r = client.post("/batch/excel-jobs",
                    files={"file": ("input.xlsx", _xlsx(), XLSX)},
                    data={"target_languages": "hi-IN"})

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert "busy" in r.json()["detail"]
    assert _active_ids(client) == before
    # The two admitted sheets are the jobs' own (the fake runner never reads
    # them); the turned-away one left nothing.
    assert len(_temp_files()) == 2


def test_a_story_job_beyond_the_waiting_cap_gets_503_with_retry_after(full_queue) -> None:
    client, _gate, _ids = full_queue
    before = _active_ids(client)

    r = client.post("/batch/story-jobs", json={
        "title": "The stream",
        "activity_name": "thestreamstory",
        "lines": [{"text": "Hi there!", "emotion": "Cheerful"}],
        "target_languages": ["hi-IN"],
    })

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert _active_ids(client) == before


def test_room_in_the_line_admits_again(full_queue) -> None:
    client, gate, ids = full_queue
    gate.release(ids[0])
    _settle(client, ids, [("completed", None), ("running", None)])

    _submit_story(client)  # asserts 202


def test_a_sheet_the_reader_cannot_parse_is_left_to_the_job(client, monkeypatch) -> None:
    """Admission only counts rows; a bad sheet still fails inside the job, with
    the reader's own message, as it did before admission existed."""
    monkeypatch.setenv("MAX_SHEET_ROWS", "1")
    r = _post(client, b"not a workbook")
    assert r.status_code == 202, r.text


def _disk_free_gb(monkeypatch, gb: float) -> None:
    from api import admission
    monkeypatch.setattr(admission, "free_disk_bytes", lambda _path: int(gb * admission.GB))


def test_an_excel_job_on_a_nearly_full_disk_gets_507_and_creates_no_job(client, monkeypatch) -> None:
    _disk_free_gb(monkeypatch, 3.25)

    r = _post(client, _sheet(1))

    assert r.status_code == 507
    assert r.json()["detail"] == (
        "The server is almost out of disk space (3.2 GB free); "
        "ask the maintainer to clear space before starting new runs."
    )
    assert _active_ids(client) == set()
    assert client.ran == []
    assert _temp_files() == []


def test_a_story_job_on_a_nearly_full_disk_gets_507_and_creates_no_job(client, monkeypatch) -> None:
    _disk_free_gb(monkeypatch, 1)

    r = client.post("/batch/story-jobs", json={
        "title": "The stream",
        "activity_name": "thestreamstory",
        "lines": [{"text": "Hi there!", "emotion": "Cheerful"}],
        "target_languages": ["hi-IN"],
    })

    assert r.status_code == 507
    assert "almost out of disk space" in r.json()["detail"]
    assert _active_ids(client) == set()


def test_the_free_disk_threshold_comes_from_the_env(client, monkeypatch) -> None:
    _disk_free_gb(monkeypatch, 8)
    monkeypatch.setenv("MIN_FREE_DISK_GB", "10")
    assert _post(client, _sheet(1)).status_code == 507
    monkeypatch.setenv("MIN_FREE_DISK_GB", "7.5")
    assert _post(client, _sheet(1)).status_code == 202


def test_enough_free_disk_admits_excel_and_story_jobs(client, monkeypatch) -> None:
    _disk_free_gb(monkeypatch, 5)  # exactly the default threshold is enough

    async def story_runner(*, job_id, **_kwargs):
        client.ran.append(job_id)

    monkeypatch.setattr(story_routes, "run_story_job", story_runner)

    r = _post(client, _sheet(1))
    assert r.status_code == 202, r.text
    r = client.post("/batch/story-jobs", json={
        "title": "The stream",
        "activity_name": "thestreamstory",
        "lines": [{"text": "Hi there!", "emotion": "Cheerful"}],
        "target_languages": ["hi-IN"],
    })
    assert r.status_code == 202, r.text
    assert len(_active_ids(client)) == 2


def test_free_space_just_under_the_minimum_is_not_shown_as_the_minimum(client, monkeypatch) -> None:
    _disk_free_gb(monkeypatch, 4.96)
    r = _post(client, _sheet(1))
    assert r.status_code == 507
    assert "(4.9 GB free)" in r.json()["detail"]
