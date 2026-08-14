import asyncio
import zipfile

from batch.models import ExcelRow, JobSummary
from batch.naming import _build_s3_key
from batch.service import run_excel_batch_job
from batch.store import JobsStore
from services.elevenlabs import ElevenLabsSettings
from services.email import EmailSettings
from services.nas import NasConfig
from services.qc import QCSettings
from services.s3 import S3Config
from services.sarvam import SarvamSettings
from services.settings import BatchSettings, Settings


def _settings(*, upload_to_s3: bool = True, english_voice: str = "english-voice") -> Settings:
    """Configuration as a route would have resolved it, before the job starts."""
    return Settings(
        s3=S3Config(
            endpoint=None, access_key="ak", secret_key="sk", bucket="bucket", region="region"
        ),
        eleven=ElevenLabsSettings(
            api_key="key", desi_voice_id="desi-voice", english_voice_id=english_voice
        ),
        qc=QCSettings(api_key="gemini-key", models=["model-a"], enabled=True),
        sarvam=SarvamSettings(api_key="sarvam-key"),
        nas=NasConfig(
            mode="local", root_path="./nas_data", server="", share="",
            username="", password="", domain="", port=445,
        ),
        email=EmailSettings(api_key="", from_address="", to_addresses=()),
        batch=BatchSettings(upload_to_s3=upload_to_s3, max_language_parallelism=None),
    )


def _identity_qc_batch(
    _original_text: str,
    translations: dict[str, str],
    _target_languages: list[str],
    *,
    settings=None,
    teaching_mode=False,
) -> dict[str, str]:
    return dict(translations)


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

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    class FakeS3Client:
        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda text, language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-seq-test"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN", "ta-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.rows_processed == 2
    assert state.summary.rows_succeeded == 2
    assert state.summary.language_tasks_total == 4
    assert state.summary.language_tasks_succeeded == 4


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

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    def fake_tts(text: str, language: str, settings=None) -> bytes:
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

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", fake_tts)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-filename-emotion"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
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

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda text, language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-delete-excel"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path=str(excel_path),
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert not excel_path.exists()

def test_run_excel_batch_job_translation_failure_skips_audio(monkeypatch) -> None:
    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="a")]
    captured_tts_texts: list[str] = []

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, None, "429 rate limit"

    def fake_tts(text: str, language: str, settings=None) -> bytes:
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

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", fake_tts)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-translation-placeholder"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.translation_fallbacks == 1
    assert state.summary.language_tasks_succeeded == 0
    assert state.summary.language_tasks_failed == 1
    assert captured_tts_texts == []
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads == []


def test_run_excel_batch_job_tts_failure_skips_audio(monkeypatch) -> None:
    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="a")]

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    def failing_tts(_text: str, _language: str, runtime_config=None) -> bytes:
        raise RuntimeError("11labs timeout")

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", failing_tts)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-tts-placeholder"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.translation_fallbacks == 0
    assert state.summary.language_tasks_succeeded == 0
    assert state.summary.language_tasks_failed == 1
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads == []


def test_run_excel_batch_job_qc_failure_skips_tts(monkeypatch) -> None:
    from services.qc import QCError

    rows = [ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="a")]
    captured_tts_texts: list[str] = []

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    def fake_tts(text: str, language: str, settings=None) -> bytes:
        captured_tts_texts.append(text)
        return b"fake-audio"

    def failing_qc(
        _original_text: str,
        _translations: dict[str, str],
        _target_languages: list[str],
        *,
        metadata=None,
        runtime_config=None,
    ) -> dict[str, str]:
        raise QCError("gemini unavailable")

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", fake_tts)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", failing_qc)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-qc-failure"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.language_tasks_succeeded == 0
    assert state.summary.language_tasks_failed == 1
    assert captured_tts_texts == []
    assert FakeS3Client.last_instance is not None
    assert FakeS3Client.last_instance.uploads == []


def test_run_excel_batch_job_dedupes_duplicate_filenames(monkeypatch) -> None:
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="promo"),
        ExcelRow(row_index=3, text="row2", emotion="", activity_name="Act", audio_type="promo"),
    ]

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, audio_files, folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-dedupe-filenames"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
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


def test_run_excel_batch_job_uploads_each_activity_separately(monkeypatch) -> None:
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act 1", audio_type="a1"),
        ExcelRow(row_index=3, text="row2", emotion="", activity_name="Act 1", audio_type="a2"),
        ExcelRow(row_index=4, text="row3", emotion="", activity_name="Act 2", audio_type="b1"),
    ]

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, dict(audio_files), folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-multi-activity"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.uploads_succeeded == 2
    assert FakeS3Client.last_instance is not None
    assert len(FakeS3Client.last_instance.uploads) == 2

    first_language, first_files, first_folder = FakeS3Client.last_instance.uploads[0]
    second_language, second_files, second_folder = FakeS3Client.last_instance.uploads[1]
    assert first_language == "Hindi"
    assert second_language == "Hindi"
    assert first_folder == "act_1"
    assert second_folder == "act_2"
    assert set(first_files.keys()) == {"a1.mp3", "a2.mp3"}
    assert set(second_files.keys()) == {"b1.mp3"}


def test_run_excel_batch_job_retries_failed_cells_before_activity_upload(monkeypatch) -> None:
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act 1", audio_type="a1"),
    ]
    translate_calls = {"gu-IN": 0}

    async def flaky_translate_async(text: str, language: str, _sarvam=None):
        if language == "gu-IN":
            translate_calls["gu-IN"] += 1
            if translate_calls["gu-IN"] == 1:
                return language, None, "Server disconnected without sending a response."
        return language, f"translated:{text}:{language}", None

    class FakeS3Client:
        last_instance: "FakeS3Client | None" = None

        def __init__(self, _config):
            self.uploads: list[tuple[str, dict[str, bytes], str]] = []
            FakeS3Client.last_instance = self

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            self.uploads.append((language, dict(audio_files), folder_name))
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", flaky_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-retry-before-upload"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["gu-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.language_tasks_succeeded == 1
    assert state.summary.language_tasks_failed == 0
    assert state.summary.rows_succeeded == 1
    assert state.summary.rows_failed == 0
    assert translate_calls["gu-IN"] >= 2

    assert FakeS3Client.last_instance is not None
    assert len(FakeS3Client.last_instance.uploads) == 1
    language, files, folder = FakeS3Client.last_instance.uploads[0]
    assert language == "Gujarati"
    assert folder == "act_1"
    assert set(files.keys()) == {"a1.mp3"}


def test_run_excel_batch_job_writes_local_archives_when_s3_disabled(monkeypatch, tmp_path) -> None:
    """S3 upload turned off: audio still gets zipped, to disk instead."""
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act 1", audio_type="a1"),
    ]

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)

    settings = _settings(upload_to_s3=False)
    store = JobsStore()
    job_id = "job-local-fallback-disabled"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
            output_dir=tmp_path,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.uploads_skipped == 1
    assert state.summary.local_archives_succeeded == 1
    assert len(state.summary.archive_downloads) == 1

    archive = state.summary.archive_downloads[0]
    assert archive.reason == "s3_disabled"
    assert archive.filename == "act_1-Hindi.zip"
    assert archive.url.endswith("/batch_archives/job-local-fallback-disabled/act_1/act_1-Hindi.zip")

    with zipfile.ZipFile(archive.path) as zf:
        assert zf.namelist() == ["a1.mp3"]
        assert zf.read("a1.mp3") == b"fake-audio"


def test_run_excel_batch_job_writes_local_archives_when_s3_upload_fails(monkeypatch, tmp_path) -> None:
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act 1", audio_type="a1"),
    ]

    async def fake_translate_async(text: str, language: str, _sarvam=None):
        return language, f"translated:{text}:{language}", None

    class FailingS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language: str, audio_files: dict[str, bytes], folder_name: str):
            raise RuntimeError("network down")

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate_async)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FailingS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-local-fallback-failed"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
            output_dir=tmp_path,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())
    assert state is not None
    assert state.status == "completed"
    assert state.summary.uploads_failed == 1
    assert state.summary.local_archives_succeeded == 1
    assert len(state.summary.archive_downloads) == 1

    archive = state.summary.archive_downloads[0]
    assert archive.reason == "s3_failed"
    assert archive.error == "network down"

    with zipfile.ZipFile(archive.path) as zf:
        assert zf.namelist() == ["a1.mp3"]
        assert zf.read("a1.mp3") == b"fake-audio"


def test_retry_batches_qc_per_row_not_per_task(monkeypatch) -> None:
    """The retry pass and the main pass now run through the same voice_row, so a
    retry gets the same single batched QC call per row.

    Before they were separate implementations, retry called Gemini once per
    failed task: four failures across two rows cost four requests.
    """
    rows = [
        ExcelRow(row_index=2, text="row1", emotion="", activity_name="Act", audio_type="a1"),
        ExcelRow(row_index=3, text="row2", emotion="", activity_name="Act", audio_type="a2"),
    ]
    attempts: dict[str, int] = {}
    qc_batches: list[list[str]] = []

    async def flaky_translate(text: str, language: str, _sarvam=None):
        key = f"{text}:{language}"
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] == 1:
            return language, None, "429 rate limit"
        return language, f"translated:{text}:{language}", None

    def counting_qc(original, translations, languages, *, settings=None, teaching_mode=False):
        qc_batches.append(sorted(translations))
        return dict(translations)

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language, audio_files, folder_name):
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", flaky_translate)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", counting_qc)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-retry-qc-batching"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN", "ta-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())

    assert state.status == "completed"
    # Every language failed its first translation and succeeded on retry.
    assert state.summary.language_tasks_succeeded == 4
    assert state.summary.language_tasks_failed == 0
    assert state.summary.rows_succeeded == 2
    assert state.summary.rows_failed == 0

    # Two rows retried, one QC call each — not one per failed task.
    assert qc_batches == [["hi-IN", "ta-IN"], ["hi-IN", "ta-IN"]]


def test_collisions_accumulate_across_multiple_activities(monkeypatch) -> None:
    """Each activity gets a fresh buffer, so its collision count must be added
    to the job total rather than replacing it."""
    rows = [
        ExcelRow(row_index=2, text="r1", emotion="", activity_name="Act 1", audio_type="dup"),
        ExcelRow(row_index=3, text="r2", emotion="", activity_name="Act 1", audio_type="dup"),
        ExcelRow(row_index=4, text="r3", emotion="", activity_name="Act 2", audio_type="dup"),
        ExcelRow(row_index=5, text="r4", emotion="", activity_name="Act 2", audio_type="dup"),
    ]

    async def fake_translate(text: str, language: str, _sarvam=None):
        return language, f"t:{text}", None

    class FakeS3Client:
        def __init__(self, _config):
            pass

        def upload_language_zip(self, language, audio_files, folder_name):
            return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

    monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", _identity_qc_batch)
    monkeypatch.setattr(
        "batch.voiceover._generate_elevenlabs_audio_bytes",
        lambda _text, _language, settings=None: b"fake-audio",
    )
    monkeypatch.setattr("batch.service.read_excel_rows", lambda _path: rows)
    monkeypatch.setattr("batch.service.S3Client", FakeS3Client)

    settings = _settings()
    store = JobsStore()
    job_id = "job-collisions-two-activities"

    async def _run():
        await store.create(job_id)
        await run_excel_batch_job(
            job_id=job_id,
            excel_path="unused.xlsx",
            target_languages=["hi-IN"],
            jobs_store=store,
            settings=settings,
        )
        return await store.get(job_id)

    state = asyncio.run(_run())

    assert state.status == "completed"
    # One collision in each activity.
    assert state.summary.filename_collisions_resolved == 2
