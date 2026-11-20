"""The folder upload lock: uploads to the same S3 activity folder take turns,
uploads to different folders don't; local fallback zips land atomically."""
import asyncio
import threading
import time
import zipfile

from batch.models import ExcelRow
from batch.service import run_excel_batch_job
from batch.store import JobsStore
from tests.test_batch_service import _identity_qc_batch, _settings

GATE_TIMEOUT = 10.0


def _stub_voiceover(monkeypatch, rows_by_path: dict[str, list[ExcelRow]]) -> None:
    async def fake_translate_async(text: str, language: str, _sarvam=None, _google=None):
        return language, f"translated:{text}:{language}", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda path: rows_by_path[path])


async def _wait_until(predicate, timeout: float = GATE_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _run_jobs(store: JobsStore, jobs: list[tuple[str, str]], settings, **kwargs):
    for job_id, _ in jobs:
        await store.create(job_id)
    return [
        asyncio.create_task(
            run_excel_batch_job(
                job_id=job_id,
                excel_path=path,
                target_languages=["hi-IN"],
                jobs_store=store,
                settings=settings,
                **kwargs,
            )
        )
        for job_id, path in jobs
    ]


def test_two_jobs_appending_to_the_same_folder_both_keep_their_files(monkeypatch) -> None:
    rows_by_path = {
        "a.xlsx": [ExcelRow(row_index=2, text="a", emotion="", activity_name="act_1", audio_type="a1")],
        "b.xlsx": [ExcelRow(row_index=2, text="b", emotion="", activity_name="act_1", audio_type="b1")],
    }
    _stub_voiceover(monkeypatch, rows_by_path)

    bucket: dict[str, dict[str, bytes]] = {"act_1/Hindi.zip": {"old.mp3": b"old"}}
    bucket_lock = threading.Lock()
    entered: list[str] = []
    release = threading.Event()

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def folder_exists(self, folder_name: str) -> bool:
            return any(key.startswith(folder_name + "/") for key in bucket)

        def list_zip_filenames(self, folder_name: str, language: str) -> set[str]:
            with bucket_lock:
                return set(bucket.get(f"{folder_name}/{language}.zip", {}))

        def append_to_language_zip(self, language: str, new_files: dict[str, bytes], folder_name: str):
            key = f"{folder_name}/{language}.zip"
            with bucket_lock:
                existing = dict(bucket.get(key, {}))  # read
                entered.append(key)
            release.wait(GATE_TIMEOUT)  # a slow merge-and-upload
            merged = {**existing, **new_files}
            with bucket_lock:
                bucket[key] = merged  # write back
            return {"bucket": "fake", "key": key, "etag": "etag", "total_files": str(len(merged))}

    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)
    store = JobsStore()

    async def _run():
        tasks = await _run_jobs(
            store, [("job-a", "a.xlsx"), ("job-b", "b.xlsx")], _settings(), mode="append"
        )
        rows_done: dict[str, int] = {}

        async def _poll_rows_done() -> None:
            for job_id in ("job-a", "job-b"):
                state = await store.get(job_id)
                rows_done[job_id] = state.summary.rows_processed if state else 0

        try:
            assert await _wait_until(lambda: len(entered) >= 1)
            # Wait until both jobs have voiced their row, i.e. both are past the
            # row loop and into their upload; then give the second job every
            # chance to start its own read-modify-write.
            deadline = time.monotonic() + GATE_TIMEOUT
            while time.monotonic() < deadline:
                await _poll_rows_done()
                if all(done == 1 for done in rows_done.values()):
                    break
                await asyncio.sleep(0.01)
            assert rows_done == {"job-a": 1, "job-b": 1}
            await asyncio.sleep(0.3)
            entered_while_first_held = len(entered)
        finally:
            release.set()
        await asyncio.gather(*tasks)
        return entered_while_first_held

    entered_while_first_held = asyncio.run(_run())

    assert entered_while_first_held == 1
    assert set(bucket["act_1/Hindi.zip"]) == {"old.mp3", "a1.mp3", "b1.mp3"}


def test_jobs_uploading_to_different_folders_run_at_the_same_time(monkeypatch) -> None:
    rows_by_path = {
        "a.xlsx": [ExcelRow(row_index=2, text="a", emotion="", activity_name="Act A", audio_type="a1")],
        "b.xlsx": [ExcelRow(row_index=2, text="b", emotion="", activity_name="Act B", audio_type="b1")],
    }
    _stub_voiceover(monkeypatch, rows_by_path)

    entered: list[str] = []
    entered_lock = threading.Lock()
    release = threading.Event()

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            with entered_lock:
                entered.append(folder_name)
            release.wait(GATE_TIMEOUT)
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)
    store = JobsStore()

    async def _run():
        tasks = await _run_jobs(store, [("job-a", "a.xlsx"), ("job-b", "b.xlsx")], _settings())
        try:
            both_entered = await _wait_until(lambda: len(entered) == 2)
        finally:
            release.set()
        await asyncio.gather(*tasks)
        return both_entered

    assert asyncio.run(_run()) is True
    assert sorted(entered) == ["act_a", "act_b"]


def test_local_archive_is_written_in_place_with_no_temp_file_left(monkeypatch, tmp_path) -> None:
    rows_by_path = {
        "a.xlsx": [ExcelRow(row_index=2, text="a", emotion="", activity_name="Act 1", audio_type="a1")],
    }
    _stub_voiceover(monkeypatch, rows_by_path)
    store = JobsStore()

    async def _run():
        tasks = await _run_jobs(
            store, [("job-local", "a.xlsx")], _settings(upload_to_s3=False), output_dir=tmp_path
        )
        await asyncio.gather(*tasks)
        return await store.get("job-local")

    state = asyncio.run(_run())
    assert state is not None and state.status == "completed"
    [archive] = state.summary.archive_downloads
    expected = tmp_path / "batch_archives" / "job-local" / "act_1" / "act_1-Hindi.zip"
    assert archive.filename == "act_1-Hindi.zip"
    assert archive.path == str(expected)
    assert archive.url == "/output/batch_archives/job-local/act_1/act_1-Hindi.zip"
    with zipfile.ZipFile(expected) as zf:
        assert zf.namelist() == ["a1.mp3"]
    assert list(tmp_path.rglob("*.tmp")) == []


def test_lock_spans_every_language_of_one_jobs_upload(monkeypatch) -> None:
    """Two create-mode jobs on the same folder: one job's Hindi and Tamil zips
    both go up before the other job's first zip does."""
    rows_by_path = {
        "a.xlsx": [ExcelRow(row_index=2, text="a", emotion="", activity_name="Act 1", audio_type="a1")],
        "b.xlsx": [ExcelRow(row_index=2, text="b", emotion="", activity_name="Act 1", audio_type="b1")],
    }
    _stub_voiceover(monkeypatch, rows_by_path)

    calls: list[tuple[str, str]] = []
    calls_lock = threading.Lock()

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            with calls_lock:
                calls.append((language, next(iter(audio_files))))
            time.sleep(0.1)  # a slow upload: without the lock the jobs interleave
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)
    store = JobsStore()

    async def _run():
        tasks = []
        for job_id, path in (("job-a", "a.xlsx"), ("job-b", "b.xlsx")):
            await store.create(job_id)
            tasks.append(asyncio.create_task(run_excel_batch_job(
                job_id=job_id, excel_path=path, target_languages=["hi-IN", "ta-IN"],
                jobs_store=store, settings=_settings(),
            )))
        await asyncio.gather(*tasks)

    asyncio.run(_run())

    assert len(calls) == 4
    first_job, second_job = calls[0][1], calls[2][1]
    assert {first_job, second_job} == {"a1.mp3", "b1.mp3"}
    assert [file for _, file in calls] == [first_job, first_job, second_job, second_job]
