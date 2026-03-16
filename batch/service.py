from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import uuid

from batch.models import JobSummary
from services.elevenlabs import get_batch_default_config, get_elevenlabs_api_key, synthesize_speech_bytes
from services.audio_compress import compress_mp3_bytes
from batch.excel import read_excel_rows
from batch.store import JobsStore
from services.translation import translate_with_fallback
from services.wasabi import S3Client, S3ConfigError, get_s3_config
from services.qc import qc_translations_batch, QCError, LANGUAGE_NAMES

logger = logging.getLogger(__name__)


def _should_upload_to_s3() -> bool:
    return os.getenv("BATCH_ENABLE_WASABI_UPLOAD", "").strip().lower() in {"1", "true", "yes", "on"}


def _should_enable_qc() -> bool:
    return os.getenv("BATCH_ENABLE_QC", "").strip().lower() in {"1", "true", "yes", "on"}


def _sanitize_for_key(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw.strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "na"


def _build_s3_key(job_id: str, target_language: str, audio_type: str) -> str:
    audio_type_fragment = _sanitize_for_key(audio_type)
    return f"batch/{job_id}/{target_language}/{audio_type_fragment}-{uuid.uuid4().hex}.mp3"


def _generate_elevenlabs_audio_bytes(text: str) -> bytes:
    return synthesize_speech_bytes(
        text,
        api_key=get_elevenlabs_api_key(),
        config=get_batch_default_config(),
    )


async def _translate_language_async(
    text: str, language: str
) -> tuple[str, str | None, str | None]:
    """Returns (language, translated_text, error). Exactly one of the last two will be None."""
    try:
        translated = await asyncio.to_thread(
            translate_with_fallback,
            text,
            target_language_code=language,
            source_language_code="auto",
        )
        return language, translated, None
    except Exception as exc:
        return language, None, str(exc)


async def run_excel_batch_job(
    *,
    job_id: str,
    excel_path: str,
    target_languages: list[str],
    jobs_store: JobsStore,
) -> None:
    summary = JobSummary()
    summary.started_at = await jobs_store.start(job_id)
    logger.info("Job %s started | excel=%s | languages=%s", job_id, excel_path, target_languages)

    try:
        upload_to_s3 = _should_upload_to_s3()
        s3_client: S3Client | None = None
        if upload_to_s3:
            config = await asyncio.to_thread(get_s3_config)
            s3_client = S3Client(config)

        try:
            rows = await asyncio.to_thread(read_excel_rows, excel_path)
        finally:
            excel_file = Path(excel_path)
            if excel_file.exists():
                try:
                    excel_file.unlink()
                except OSError as exc:
                    logger.warning(
                        "Job %s: failed to delete temp excel file %s: %s",
                        job_id,
                        excel_path,
                        exc,
                    )
        summary.total_rows = len(rows)
        summary.language_tasks_total = len(rows) * len(target_languages)
        await jobs_store.update_summary(job_id, summary)
        logger.info(
            "Job %s | %d rows × %d languages = %d tasks",
            job_id, len(rows), len(target_languages), summary.language_tasks_total,
        )
    except S3ConfigError as exc:
        await jobs_store.fail(job_id, f"AWS S3 config error: {exc}", summary)
        return
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch setup failed: {exc}", summary)
        return

    if not rows:
        await jobs_store.complete(job_id, summary)
        return

    # Extract activity_name from first row for S3 folder naming
    activity_name = _sanitize_for_key(rows[0].activity_name) if rows else "batch"
    
    # Collect audio files by language for zip upload
    language_audio_files: dict[str, dict[str, bytes]] | None = (
        {lang: {} for lang in target_languages} if s3_client else None
    )

    try:
        for row in rows:
            logger.info(
                "Job %s | row %d: translating into %d languages in parallel",
                job_id, row.row_index, len(target_languages),
            )

            # Phase 1 — translate all languages concurrently
            translation_results: list[tuple[str, str | None, str | None]] = await asyncio.gather(
                *[_translate_language_async(row.text, lang) for lang in target_languages]
            )

            # Phase 1.5 — QC all translations (optional)
            if _should_enable_qc():
                # Collect successful translations
                translations_to_qc = {
                    lang: text for lang, text, error in translation_results if error is None and text
                }
                
                if translations_to_qc:
                    try:
                        logger.info(
                            "Job %s | row %d: QC start for %d languages",
                            job_id, row.row_index, len(translations_to_qc),
                        )
                        qc_results = await asyncio.to_thread(
                            qc_translations_batch,
                            row.text,
                            translations_to_qc,
                            list(translations_to_qc.keys()),
                            metadata={
                                "job_id": job_id,
                                "row_index": row.row_index,
                                "activity_name": row.activity_name,
                                "voiceover_title": row.audio_type,
                            },
                        )
                        
                        # Update translation_results with QC'd texts
                        translation_results = [
                            (lang, qc_results.get(lang, text), error)
                            if error is None and text
                            else (lang, text, error)
                            for lang, text, error in translation_results
                        ]
                        logger.info(
                            "Job %s | row %d: QC complete",
                            job_id, row.row_index,
                        )
                    except QCError as exc:
                        logger.warning(
                            "Job %s | row %d: QC failed, using original translations: %s",
                            job_id, row.row_index, exc,
                        )
                        # translation_results stays unchanged, continue with originals

            # Phase 2 — TTS sequentially, one language at a time
            row_ok = True
            for language, translated_text, translation_error in translation_results:
                if translation_error is not None:
                    logger.error(
                        "Job %s | row %d | lang %s: translation failed: %s",
                        job_id, row.row_index, language, translation_error,
                    )
                    summary.language_tasks_failed += 1
                    if s3_client:
                        summary.uploads_failed += 1
                    row_ok = False
                    await jobs_store.update_summary(job_id, summary)
                    continue

                logger.info("Job %s | row %d | lang %s: TTS start", job_id, row.row_index, language)
                try:
                    tts_text = (
                        f"[{row.emotion}] {translated_text}"
                        if row.emotion
                        else translated_text
                    )
                    audio_bytes = await asyncio.to_thread(_generate_elevenlabs_audio_bytes, tts_text)
                    if s3_client is not None:
                        audio_bytes = compress_mp3_bytes(audio_bytes)

                    filename = row.audio_type
                    if not filename:
                        logger.warning(
                            "Job %s | row %d | lang %s: empty voiceover_title; using .mp3",
                            job_id,
                            row.row_index,
                            language,
                        )
                    if not filename.lower().endswith(".mp3"):
                        filename = f"{filename}.mp3"

                    if s3_client is None:
                        summary.language_tasks_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: done (discarded audio; no upload)",
                            job_id, row.row_index, language,
                        )
                    else:
                        # Collect for zip upload
                        if language_audio_files is not None:
                            language_audio_files[language][filename] = audio_bytes
                        summary.language_tasks_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: ready for zip → %s",
                            job_id, row.row_index, language, filename,
                        )
                    await jobs_store.update_summary(job_id, summary)
                except Exception as exc:
                    logger.error(
                        "Job %s | row %d | lang %s: TTS failed: %s",
                        job_id, row.row_index, language, exc,
                    )
                    summary.language_tasks_failed += 1
                    if s3_client:
                        summary.uploads_failed += 1
                    row_ok = False
                    await jobs_store.update_summary(job_id, summary)

            summary.rows_processed += 1
            if row_ok:
                summary.rows_succeeded += 1
            else:
                summary.rows_failed += 1

            logger.info(
                "Job %s | row %d complete | tasks succeeded=%d failed=%d",
                job_id, row.row_index, summary.language_tasks_succeeded, summary.language_tasks_failed,
            )
            await jobs_store.update_summary(job_id, summary)

        # Step 3 — Upload zip files for each language
        if s3_client is not None and language_audio_files is not None:
            logger.info("Job %s | uploading zip files for %d languages under %s", job_id, len(target_languages), activity_name)
            
            for language in target_languages:
                audio_files = language_audio_files[language]
                if not audio_files:
                    logger.info("Job %s | lang %s: no files to zip", job_id, language)
                    continue
                
                try:
                    language_label = LANGUAGE_NAMES.get(language, language)
                    result = await asyncio.to_thread(
                        s3_client.upload_language_zip,
                        language_label,
                        audio_files,
                        activity_name
                    )
                    summary.uploads_succeeded += 1
                    logger.info(
                        "Job %s | lang %s: uploaded zip with %d files → %s",
                        job_id, language_label, len(audio_files), result["key"]
                    )
                except Exception as exc:
                    logger.error(
                        "Job %s | lang %s: zip upload failed: %s",
                        job_id, language, exc,
                    )
                    summary.uploads_failed += 1

        await jobs_store.complete(job_id, summary)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch execution failed: {exc}", summary)
