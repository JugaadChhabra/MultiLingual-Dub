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

    class FakeS3Client:
        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", lambda text: b"fake-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: object())
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

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


def test_run_excel_batch_job_fails_when_s3_config_missing(monkeypatch) -> None:
    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="", audio_type="a")]

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", lambda text: b"fake-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: (_ for _ in ()).throw(RuntimeError("missing config")))

    store = JobsStore()
    job_id = "job-missing-s3"

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


def test_run_excel_batch_job_uses_voiceover_title_and_emotion(monkeypatch) -> None:
    rows = [
        ExcelRow(
            row_index=2,
            text="row1",
            emotion="excited",
            activity_name="Act",
            audio_type="My File",
        )
    ]

    captured_texts: list[str] = []

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    def fake_tts(text: str) -> bytes:
        captured_texts.append(text)
        return b"fake-audio"

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", fake_tts)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: object())
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    store = JobsStore()
    job_id = "job-filename-emotion"

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
    assert state.status == "completed"
    assert captured_texts == ["[excited] translated:row1:hi-IN"]
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads
    language_label, audio_files, _ = FakeS3Client.last_instance.uploads[0]
    assert language_label == "Hindi"
    assert list(audio_files.keys()) == ["My File.mp3"]


def test_run_excel_batch_job_deletes_excel_file(monkeypatch, tmp_path) -> None:
    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="", audio_type="a")]
    excel_path = tmp_path / "input.xlsx"
    excel_path.write_text("placeholder", encoding="utf-8")

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", lambda text: b"fake-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: object())

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    store = JobsStore()
    job_id = "job-delete-excel"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path=str(excel_path),
            target_languages=["hi-IN"],
            jobs_store=store,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert not excel_path.exists()

def test_run_excel_batch_job_sets_failed_when_s3_missing(monkeypatch) -> None:
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: (_ for _ in ()).throw(RuntimeError("missing config")))

    store = JobsStore()
    job_id = "job-missing-s3"

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


def test_run_excel_batch_job_translation_failure_creates_placeholder_audio(monkeypatch) -> None:
    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="a")]
    captured_tts_texts: list[str] = []

    async def fake_translate_async(text: str, language: str):
        return language, None, "429 rate limit"

    def fake_tts(text: str) -> bytes:
        captured_tts_texts.append(text)
        return b"fake-audio"

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", fake_tts)
    monkeypatch.setattr("batch.service._build_placeholder_mp3_bytes", lambda: b"placeholder-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: object())
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    store = JobsStore()
    job_id = "job-translation-placeholder"

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
    assert state.status == "completed"
    assert state.summary.translation_fallbacks == 1
    assert state.summary.placeholder_audio_generated == 1
    assert state.summary.language_tasks_succeeded == 1
    assert state.summary.language_tasks_failed == 0
    assert captured_tts_texts == []
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads
    _, audio_files, _ = FakeS3Client.last_instance.uploads[0]
    assert audio_files["a.mp3"] == b"placeholder-audio"


def test_run_excel_batch_job_tts_failure_creates_placeholder_audio(monkeypatch) -> None:
    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="a")]

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    def failing_tts(_text: str) -> bytes:
        raise RuntimeError("11labs timeout")

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", failing_tts)
    monkeypatch.setattr("batch.service._build_placeholder_mp3_bytes", lambda: b"placeholder-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: object())
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    store = JobsStore()
    job_id = "job-tts-placeholder"

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
    assert state.status == "completed"
    assert state.summary.translation_fallbacks == 0
    assert state.summary.placeholder_audio_generated == 1
    assert state.summary.language_tasks_succeeded == 1
    assert state.summary.language_tasks_failed == 0
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads
    _, audio_files, _ = FakeS3Client.last_instance.uploads[0]
    assert audio_files["a.mp3"] == b"placeholder-audio"


def test_run_excel_batch_job_continues_after_unexpected_row_error(monkeypatch) -> None:
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="", audio_type="a"),
        ExcelRow(row_index=3, text="row2", emotion="", activity_name="", audio_type="b"),
    ]

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    def fake_tts(_text: str) -> bytes:
        return b"fake-audio"

    call_count = {"n": 0}

    def flaky_should_enable_qc(runtime_config=None) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("temporary QC toggle failure")
        return False

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", fake_tts)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service._should_enable_qc", flaky_should_enable_qc)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "false")

    store = JobsStore()
    job_id = "job-row-continue"

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
    assert state.status == "completed"
    assert state.summary.rows_processed == 2
    assert state.summary.rows_succeeded == 2
    assert state.summary.rows_failed == 0
    assert state.summary.unexpected_row_errors == 0
    assert state.summary.language_tasks_succeeded == 2


def test_run_excel_batch_job_dedupes_duplicate_filenames(monkeypatch) -> None:
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="promo"),
        ExcelRow(row_index=3, text="row2", emotion="", activity_name="Act", audio_type="promo"),
    ]

    async def fake_translate_async(text: str, language: str):
        return language, f"translated:{text}:{language}", None

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.service._generate_elevenlabs_audio_bytes", lambda _text: b"fake-audio")
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setenv("BATCH_ENABLE_WASABI_UPLOAD", "true")
    monkeypatch.setattr("batch.service.get_s3_config", lambda runtime_config=None: object())
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    store = JobsStore()
    job_id = "job-dedupe-filenames"

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
    assert state.status == "completed"
    assert state.summary.filename_collisions_resolved == 1
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads

    language_label, audio_files, _ = FakeS3Client.last_instance.uploads[0]
    assert language_label == "Hindi"
    assert len(audio_files) == 2
    assert "promo.mp3" in audio_files
    assert any(name.startswith("promo-row3") for name in audio_files)
