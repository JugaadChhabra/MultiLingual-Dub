from __future__ import annotations

import asyncio
import io
import logging
import os
from functools import lru_cache
from pathlib import Path
import uuid

from batch.excel import read_excel_rows
from batch.models import JobSummary
from batch.store import JobsStore
from services.audio_compress import compress_mp3_bytes
from services.elevenlabs import get_batch_config_for_language, get_elevenlabs_api_key, synthesize_speech_bytes
from services.qc import LANGUAGE_NAMES, QCError, qc_translations_batch
from services.runtime_config import RuntimeConfig, get_config_value
from services.translation import translate_with_fallback
from services.wasabi import S3Client, S3ConfigError, get_s3_config

logger = logging.getLogger(__name__)


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _should_upload_to_s3(runtime_config: RuntimeConfig | None = None) -> bool:
    return _is_truthy(get_config_value("BATCH_ENABLE_WASABI_UPLOAD", runtime_config=runtime_config))


def _should_enable_qc(runtime_config: RuntimeConfig | None = None) -> bool:
    return _is_truthy(get_config_value("BATCH_ENABLE_QC", runtime_config=runtime_config))


def _sanitize_for_key(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw.strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "na"


def _build_s3_key(job_id: str, target_language: str, audio_type: str) -> str:
    audio_type_fragment = _sanitize_for_key(audio_type)
    return f"batch/{job_id}/{target_language}/{audio_type_fragment}-{uuid.uuid4().hex}.mp3"


def _generate_elevenlabs_audio_bytes(
    text: str,
    language: str,
    runtime_config: RuntimeConfig | None = None,
) -> bytes:
    return synthesize_speech_bytes(
        text,
        api_key=get_elevenlabs_api_key(runtime_config=runtime_config),
        config=get_batch_config_for_language(language, runtime_config=runtime_config),
    )


def _validate_english_voice_if_needed(
    target_languages: list[str],
    runtime_config: RuntimeConfig | None = None,
) -> None:
    has_english_target = any(language.strip().lower().startswith("en") for language in target_languages)
    if not has_english_target:
        return
    english_voice = get_config_value("ENGLISH_VOICE", runtime_config=runtime_config)
    if not english_voice:
        raise ValueError("ENGLISH_VOICE is required when generating English batch audio")


@lru_cache(maxsize=1)
def _build_placeholder_mp3_bytes() -> bytes:
    """
    Build a short silent MP3 once and reuse it whenever translation/TTS fails.
    This guarantees we still produce a file for every expected voiceover title.
    """
    default_bytes = b"ID3\x04\x00\x00\x00\x00\x00\x00"  # last-resort marker if MP3 tooling is unavailable
    try:
        from pydub import AudioSegment
        from pydub.utils import which
    except Exception as exc:
        logger.warning("Placeholder audio fallback: pydub unavailable (%s)", exc)
        return default_bytes

    ffmpeg_path = os.getenv("AUDIO_COMPRESS_FFMPEG_PATH", "").strip() or os.getenv(
        "FFMPEG_PATH", ""
    ).strip()
    if not ffmpeg_path:
        ffmpeg_path = which("ffmpeg") or which("avconv")
    if not ffmpeg_path:
        try:
            import imageio_ffmpeg

            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            logger.warning("Placeholder audio fallback: bundled ffmpeg unavailable (%s)", exc)
            ffmpeg_path = ""

    if not ffmpeg_path:
        logger.warning("Placeholder audio fallback: ffmpeg unavailable; using marker bytes")
        return default_bytes

    AudioSegment.converter = ffmpeg_path
    duration_ms_raw = os.getenv("BATCH_PLACEHOLDER_AUDIO_MS", "350").strip() or "350"
    try:
        duration_ms = max(100, min(5000, int(duration_ms_raw)))
    except ValueError:
        duration_ms = 350

    try:
        segment = AudioSegment.silent(duration=duration_ms).set_frame_rate(22050).set_channels(1)
        buf = io.BytesIO()
        segment.export(buf, format="mp3", bitrate="32k", codec="libmp3lame")
        data = buf.getvalue()
        if data:
            return data
    except Exception as exc:
        logger.warning("Placeholder audio generation failed: %s", exc)
    return default_bytes


def _generate_placeholder_audio_bytes(*, job_id: str, row_index: int, language: str, reason: str) -> bytes:
    logger.warning(
        "Job %s | row %d | lang %s: generating placeholder MP3 (%s)",
        job_id,
        row_index,
        language,
        reason,
    )
    return _build_placeholder_mp3_bytes()


async def _translate_language_async(
    text: str, language: str, runtime_config: RuntimeConfig | None = None
) -> tuple[str, str | None, str | None]:
    """Returns (language, translated_text, error). Exactly one of the last two will be None."""
    try:
        translated = await asyncio.to_thread(
            translate_with_fallback,
            text,
            runtime_config=runtime_config,
            target_language_code=language,
            source_language_code="auto",
        )
        return language, translated, None
    except Exception as exc:
        return language, None, str(exc)


def _dedupe_filename(
    filename: str,
    existing_files: dict[str, bytes],
    row_index: int,
) -> tuple[str, bool]:
    if filename not in existing_files:
        return filename, False

    stem = Path(filename).stem or "audio"
    suffix = Path(filename).suffix or ".mp3"
    candidate = f"{stem}-row{row_index}{suffix}"
    counter = 2
    while candidate in existing_files:
        candidate = f"{stem}-row{row_index}-{counter}{suffix}"
        counter += 1
    return candidate, True


async def run_excel_batch_job(
    *,
    job_id: str,
    excel_path: str,
    target_languages: list[str],
    jobs_store: JobsStore,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    """
    Run a batch job to completion without a fixed global timeout.
    Long-running jobs remain active until they finish or hit a terminal error.
    """
    summary = JobSummary()
    try:
        await _run_batch_job_impl(
            job_id,
            excel_path,
            target_languages,
            jobs_store,
            summary,
            runtime_config=runtime_config,
        )
    except Exception as exc:
        logger.exception("Job %s crashed unexpectedly: %s", job_id, exc)
        await jobs_store.fail(job_id, f"Batch execution crashed: {exc}", summary)


async def _run_batch_job_impl(
    job_id: str,
    excel_path: str,
    target_languages: list[str],
    jobs_store: JobsStore,
    summary: JobSummary,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    summary.started_at = await jobs_store.start(job_id)
    logger.info("Job %s started | excel=%s | languages=%s", job_id, excel_path, target_languages)

    try:
        upload_to_s3 = _should_upload_to_s3(runtime_config=runtime_config)
        _validate_english_voice_if_needed(target_languages, runtime_config=runtime_config)
        s3_client: S3Client | None = None
        if upload_to_s3:
            config = await asyncio.to_thread(get_s3_config, runtime_config)
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
            "Job %s | %d rows x %d languages = %d tasks",
            job_id,
            len(rows),
            len(target_languages),
            summary.language_tasks_total,
        )
    except S3ConfigError as exc:
        await jobs_store.fail(job_id, f"Wasabi config error: {exc}", summary)
        return
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch setup failed: {exc}", summary)
        return

    if not rows:
        await jobs_store.complete(job_id, summary)
        return

    activity_name = _sanitize_for_key(rows[0].activity_name) if rows else "batch"
    language_audio_files: dict[str, dict[str, bytes]] | None = (
        {lang: {} for lang in target_languages} if s3_client else None
    )

    try:
        for row in rows:
            row_ok = True
            try:
                logger.info(
                    "Job %s | row %d: translating into %d languages in parallel",
                    job_id,
                    row.row_index,
                    len(target_languages),
                )

                translation_tasks = []
                for language in target_languages:
                    if runtime_config is None:
                        translation_tasks.append(_translate_language_async(row.text, language))
                    else:
                        translation_tasks.append(
                            _translate_language_async(row.text, language, runtime_config=runtime_config)
                        )

                translation_raw_results = await asyncio.gather(*translation_tasks, return_exceptions=True)
                translation_results: list[tuple[str, str | None, str | None]] = []
                for index, result in enumerate(translation_raw_results):
                    language = target_languages[index]
                    if isinstance(result, Exception):
                        logger.error(
                            "Job %s | row %d | lang %s: translation task crashed: %s",
                            job_id,
                            row.row_index,
                            language,
                            result,
                        )
                        translation_results.append((language, None, str(result)))
                        continue

                    if isinstance(result, tuple) and len(result) == 3:
                        translation_results.append(result)
                        continue

                    translation_results.append(
                        (language, None, f"Unexpected translation result type: {type(result).__name__}")
                    )

                qc_enabled = False
                try:
                    qc_enabled = _should_enable_qc(runtime_config=runtime_config)
                except Exception as exc:
                    logger.error(
                        "Job %s | row %d: failed to read QC toggle, continuing without QC: %s",
                        job_id,
                        row.row_index,
                        exc,
                    )

                if qc_enabled:
                    translations_to_qc = {
                        lang: text for lang, text, error in translation_results if error is None and text
                    }

                    if translations_to_qc:
                        try:
                            logger.info(
                                "Job %s | row %d: QC start for %d languages",
                                job_id,
                                row.row_index,
                                len(translations_to_qc),
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
                                runtime_config=runtime_config,
                            )
                            translation_results = [
                                (lang, qc_results.get(lang, text), error)
                                if error is None and text
                                else (lang, text, error)
                                for lang, text, error in translation_results
                            ]
                            logger.info("Job %s | row %d: QC complete", job_id, row.row_index)
                        except QCError as exc:
                            logger.warning(
                                "Job %s | row %d: QC failed, using original translations: %s",
                                job_id,
                                row.row_index,
                                exc,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Job %s | row %d: unexpected QC error, using original translations: %s",
                                job_id,
                                row.row_index,
                                exc,
                            )

                for language, translated_text, translation_error in translation_results:
                    used_placeholder = False
                    effective_text = (translated_text or "").strip()

                    if translation_error is not None or not effective_text:
                        used_placeholder = True
                        summary.translation_fallbacks += 1
                        audio_bytes = _generate_placeholder_audio_bytes(
                            job_id=job_id,
                            row_index=row.row_index,
                            language=language,
                            reason=translation_error or "empty translation result",
                        )
                    else:
                        logger.info("Job %s | row %d | lang %s: TTS start", job_id, row.row_index, language)
                        tts_text = f"[{row.emotion}] {effective_text}" if row.emotion else effective_text
                        try:
                            if runtime_config is None:
                                audio_bytes = await asyncio.to_thread(
                                    _generate_elevenlabs_audio_bytes,
                                    tts_text,
                                    language,
                                )
                            else:
                                audio_bytes = await asyncio.to_thread(
                                    _generate_elevenlabs_audio_bytes,
                                    tts_text,
                                    language,
                                    runtime_config,
                                )
                        except Exception as exc:
                            used_placeholder = True
                            audio_bytes = _generate_placeholder_audio_bytes(
                                job_id=job_id,
                                row_index=row.row_index,
                                language=language,
                                reason=f"TTS failure: {exc}",
                            )

                    if used_placeholder:
                        summary.placeholder_audio_generated += 1
                    elif s3_client is not None:
                        audio_bytes = compress_mp3_bytes(audio_bytes)

                    filename = row.audio_type or f"row-{row.row_index}-{language}"
                    if not filename.lower().endswith(".mp3"):
                        filename = f"{filename}.mp3"

                    if s3_client is None:
                        summary.language_tasks_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: done (discarded audio; no upload)%s",
                            job_id,
                            row.row_index,
                            language,
                            " [placeholder]" if used_placeholder else "",
                        )
                    else:
                        final_name_for_log = filename
                        if language_audio_files is not None:
                            final_filename, had_collision = _dedupe_filename(
                                filename,
                                language_audio_files[language],
                                row.row_index,
                            )
                            if had_collision:
                                summary.filename_collisions_resolved += 1
                                logger.warning(
                                    "Job %s | row %d | lang %s: duplicate filename '%s' renamed to '%s'",
                                    job_id,
                                    row.row_index,
                                    language,
                                    filename,
                                    final_filename,
                                )
                            language_audio_files[language][final_filename] = audio_bytes
                            final_name_for_log = final_filename

                        summary.language_tasks_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: ready for zip -> %s%s",
                            job_id,
                            row.row_index,
                            language,
                            final_name_for_log,
                            " [placeholder]" if used_placeholder else "",
                        )
                    await jobs_store.update_summary(job_id, summary)
            except Exception as exc:
                row_ok = False
                summary.unexpected_row_errors += 1
                logger.exception("Job %s | row %d: unexpected row failure: %s", job_id, row.row_index, exc)
            finally:
                summary.rows_processed += 1
                if row_ok:
                    summary.rows_succeeded += 1
                else:
                    summary.rows_failed += 1

                logger.info(
                    "Job %s | row %d complete | tasks succeeded=%d failed=%d",
                    job_id,
                    row.row_index,
                    summary.language_tasks_succeeded,
                    summary.language_tasks_failed,
                )
                await jobs_store.update_summary(job_id, summary)

        if s3_client is not None and language_audio_files is not None:
            logger.info(
                "Job %s | uploading zip files for %d languages under %s",
                job_id,
                len(target_languages),
                activity_name,
            )

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
                        activity_name,
                    )
                    summary.uploads_succeeded += 1
                    logger.info(
                        "Job %s | lang %s: uploaded zip with %d files -> %s",
                        job_id,
                        language_label,
                        len(audio_files),
                        result["key"],
                    )
                except Exception as exc:
                    logger.error("Job %s | lang %s: zip upload failed: %s", job_id, language, exc)
                    summary.uploads_failed += 1

        await jobs_store.complete(job_id, summary)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch execution failed: {exc}", summary)
