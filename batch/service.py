from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import uuid

from batch.models import JobSummary
from services.elevenlabs import get_batch_default_config, get_elevenlabs_api_key, synthesize_speech_bytes
from batch.excel import read_excel_rows
from batch.store import JobsStore
from services.translation import translate_with_fallback
from services.wasabi import WasabiClient, WasabiConfigError, get_wasabi_config

logger = logging.getLogger(__name__)


def _should_upload_to_wasabi() -> bool:
    return os.getenv("BATCH_ENABLE_WASABI_UPLOAD", "").strip().lower() in {"1", "true", "yes", "on"}


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
        upload_to_wasabi = _should_upload_to_wasabi()
        wasabi_client: WasabiClient | None = None
        if upload_to_wasabi:
            config = await asyncio.to_thread(get_wasabi_config)
            wasabi_client = WasabiClient(config)

        rows = await asyncio.to_thread(read_excel_rows, excel_path)
        summary.total_rows = len(rows)
        summary.language_tasks_total = len(rows) * len(target_languages)
        await jobs_store.update_summary(job_id, summary)
        logger.info(
            "Job %s | %d rows × %d languages = %d tasks",
            job_id, len(rows), len(target_languages), summary.language_tasks_total,
        )
    except WasabiConfigError as exc:
        await jobs_store.fail(job_id, f"Wasabi config error: {exc}", summary)
        return
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch setup failed: {exc}", summary)
        return

    if not rows:
        await jobs_store.complete(job_id, summary)
        return

    row_output_base = Path("./output") / "batch" / job_id
    row_output_base.mkdir(parents=True, exist_ok=True)

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

            # Phase 2 — TTS sequentially, one language at a time
            row_ok = True
            for language, translated_text, translation_error in translation_results:
                if translation_error is not None:
                    logger.error(
                        "Job %s | row %d | lang %s: translation failed: %s",
                        job_id, row.row_index, language, translation_error,
                    )
                    summary.language_tasks_failed += 1
                    if wasabi_client:
                        summary.uploads_failed += 1
                    row_ok = False
                    continue

                logger.info("Job %s | row %d | lang %s: TTS start", job_id, row.row_index, language)
                try:
                    audio_bytes = await asyncio.to_thread(_generate_elevenlabs_audio_bytes, translated_text)
                    
                    # New naming convention: activity_name-audio_type-language-uuid.mp3
                    # Sanitizing names to ensure valid filenames
                    safe_activity = "".join(c for c in row.activity_name if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
                    safe_audio_type = "".join(c for c in row.audio_type if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
                    filename = f"{safe_activity}-{safe_audio_type}-{language}.mp3"
                    
                    local_file = row_output_base / filename
                    local_file.write_bytes(audio_bytes)

                    if wasabi_client is None:
                        summary.language_tasks_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: done (no upload)",
                            job_id, row.row_index, language,
                        )
                    else:
                        s3_key = _build_s3_key(job_id=job_id, target_language=language, audio_type=row.audio_type)
                        await asyncio.to_thread(wasabi_client.upload_file, local_file, s3_key)
                        summary.language_tasks_succeeded += 1
                        summary.uploads_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: done + uploaded → %s",
                            job_id, row.row_index, language, s3_key,
                        )
                except Exception as exc:
                    logger.error(
                        "Job %s | row %d | lang %s: TTS/upload failed: %s",
                        job_id, row.row_index, language, exc,
                    )
                    summary.language_tasks_failed += 1
                    if wasabi_client:
                        summary.uploads_failed += 1
                    row_ok = False

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

        await jobs_store.complete(job_id, summary)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch execution failed: {exc}", summary)
