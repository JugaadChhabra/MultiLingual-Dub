import asyncio

from batch.models import ExcelRow, JobSummary
from batch.service import _build_s3_key, run_excel_batch_job
from batch.store import JobsStore


def test_build_s3_key_contains_job_language_and_mp3() -> None:
    key = _build_s3_key("job123", "hi-IN", "Promo Audio")
    assert key.startswith("batch/job123/hi-IN/")
    assert key.endswith(".mp3")
    assert "promo_audio" in key


def test_run_excel_batch_job_processes_rows_and_languages(monkeypatch) -> None:
    """Translations run in parallel per row; TTS calls are sequential."""
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="", audio_type="a"),
        ExcelRow(row_index=3, text="row2", emotion="", activity_name="", audio_type="b"),
    ]

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", lambda text: b"fake-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service._should_upload_to_wasabi", lambda: False)

    store = JobsStore()
    job_id = "job-seq-test"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN", "ta-IN"],
            jobs_store=store,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.rows_processed == 2
    assert state.summary.rows_succeeded == 2
    assert state.summary.language_tasks_total == 4
    assert state.summary.language_tasks_succeeded == 4


def test_run_excel_batch_job_sets_failed_when_wasabi_missing(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_wasabi_config", lambda: (_ for _ in ()).throw(RuntimeError("missing config")))

    store = JobsStore()
    job_id = "job-missing-wasabi"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "failed"
    assert "Batch setup failed" in (state.error or "")
    assert isinstance(state.summary, JobSummary)
