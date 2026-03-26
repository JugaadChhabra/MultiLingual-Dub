from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
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


@dataclass(frozen=True)
class FailedLanguageTask:
    row_index: int
    row_text: str
    emotion: str
    activity_name: str
    audio_type: str
    language: str
    reason: str


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


def _validate_qc_is_enabled(runtime_config: RuntimeConfig | None = None) -> None:
    try:
        qc_enabled = _should_enable_qc(runtime_config=runtime_config)
    except Exception as exc:  # pragma: no cover - defensive branch
        raise ValueError(f"Unable to read BATCH_ENABLE_QC: {exc}") from exc
    if not qc_enabled:
        raise ValueError("BATCH_ENABLE_QC must be true: audio is generated only after Gemini QC")


def _resolve_language_parallelism(
    *,
    total_languages: int,
    requested_parallelism: int | None,
    runtime_config: RuntimeConfig | None = None,
) -> int:
    requested = requested_parallelism
    if requested is None:
        raw = get_config_value("BATCH_MAX_LANGUAGE_PARALLELISM", runtime_config=runtime_config)
        if raw:
            try:
                requested = int(raw)
            except ValueError:
                requested = None

    if requested is None:
        requested = total_languages

    requested = max(1, requested)
    return min(total_languages, requested)


async def _translate_row_languages(
    *,
    text: str,
    target_languages: list[str],
    max_parallelism: int,
    runtime_config: RuntimeConfig | None = None,
) -> list[tuple[str, str | None, str | None] | Exception]:
    semaphore = asyncio.Semaphore(max_parallelism)

    async def _translate(language: str) -> tuple[str, str | None, str | None]:
        async with semaphore:
            if runtime_config is None:
                return await _translate_language_async(text, language)
            return await _translate_language_async(text, language, runtime_config=runtime_config)

    tasks = [asyncio.create_task(_translate(language)) for language in target_languages]
    return await asyncio.gather(*tasks, return_exceptions=True)


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


def _resolve_activity_segment_name(raw_activity_name: str, current_activity_name: str | None) -> str:
    if raw_activity_name.strip():
        return _sanitize_for_key(raw_activity_name)
    if current_activity_name:
        return current_activity_name
    return "batch"


def _new_language_audio_buffers(target_languages: list[str]) -> dict[str, dict[str, bytes]]:
    return {lang: {} for lang in target_languages}


def _next_activity_folder_name(activity_name: str, upload_counts: dict[str, int]) -> str:
    next_count = upload_counts.get(activity_name, 0) + 1
    upload_counts[activity_name] = next_count
    if next_count == 1:
        return activity_name
    return f"{activity_name}-{next_count}"


async def _upload_activity_archives(
    *,
    job_id: str,
    activity_name: str,
    target_languages: list[str],
    language_audio_files: dict[str, dict[str, bytes]],
    s3_client: S3Client,
    jobs_store: JobsStore,
    summary: JobSummary,
    activity_upload_counts: dict[str, int],
) -> None:
    folder_name = _next_activity_folder_name(activity_name, activity_upload_counts)
    logger.info(
        "Job %s | activity %s: uploading zip files for %d languages under %s",
        job_id,
        activity_name,
        len(target_languages),
        folder_name,
    )

    for language in target_languages:
        audio_files = language_audio_files.get(language, {})
        if not audio_files:
            logger.info("Job %s | activity %s | lang %s: no files to zip", job_id, activity_name, language)
            continue

        try:
            language_label = LANGUAGE_NAMES.get(language, language)
            result = await asyncio.to_thread(
                s3_client.upload_language_zip,
                language_label,
                audio_files,
                folder_name,
            )
            summary.uploads_succeeded += 1
            logger.info(
                "Job %s | activity %s | lang %s: uploaded zip with %d files -> %s",
                job_id,
                activity_name,
                language_label,
                len(audio_files),
                result["key"],
            )
        except Exception as exc:
            summary.uploads_failed += 1
            logger.error(
                "Job %s | activity %s | lang %s: zip upload failed: %s",
                job_id,
                activity_name,
                language,
                exc,
            )
        finally:
            await jobs_store.update_summary(job_id, summary)


def _build_output_filename(*, audio_type: str, row_index: int, language: str) -> str:
    filename = audio_type or f"row-{row_index}-{language}"
    if not filename.lower().endswith(".mp3"):
        filename = f"{filename}.mp3"
    return filename


async def _retry_failed_activity_tasks(
    *,
    job_id: str,
    activity_name: str,
    failed_tasks: list[FailedLanguageTask],
    language_audio_files: dict[str, dict[str, bytes]],
    summary: JobSummary,
    jobs_store: JobsStore,
    row_unresolved_failures: dict[int, int],
    recoverable_failed_rows: set[int],
    runtime_config: RuntimeConfig | None = None,
) -> None:
    if not failed_tasks:
        return

    logger.info(
        "Job %s | activity %s: retrying %d failed language task(s) before upload",
        job_id,
        activity_name,
        len(failed_tasks),
    )
    for task in failed_tasks:
        try:
            if runtime_config is None:
                _, translated_text, translation_error = await _translate_language_async(
                    task.row_text,
                    task.language,
                )
            else:
                _, translated_text, translation_error = await _translate_language_async(
                    task.row_text,
                    task.language,
                    runtime_config=runtime_config,
                )
            translated_clean = (translated_text or "").strip()
            if translation_error is not None or not translated_clean:
                raise RuntimeError(translation_error or "empty translation result")

            qc_results = await asyncio.to_thread(
                qc_translations_batch,
                task.row_text,
                {task.language: translated_clean},
                [task.language],
                metadata={
                    "job_id": job_id,
                    "row_index": task.row_index,
                    "activity_name": task.activity_name,
                    "voiceover_title": task.audio_type,
                    "retry_pass": True,
                },
                runtime_config=runtime_config,
            )
            qc_text = (qc_results.get(task.language) or "").strip()
            if not qc_text:
                raise RuntimeError("QC returned empty translation")

            tts_text = f"[{task.emotion}] {qc_text}" if task.emotion else qc_text
            audio_bytes = await asyncio.to_thread(
                _generate_elevenlabs_audio_bytes,
                tts_text,
                task.language,
                runtime_config,
            )
            audio_bytes = compress_mp3_bytes(audio_bytes)

            filename = _build_output_filename(
                audio_type=task.audio_type,
                row_index=task.row_index,
                language=task.language,
            )
            final_filename, had_collision = _dedupe_filename(
                filename,
                language_audio_files[task.language],
                task.row_index,
            )
            if had_collision:
                summary.filename_collisions_resolved += 1
                logger.warning(
                    "Job %s | row %d | lang %s: retry filename collision '%s' -> '%s'",
                    job_id,
                    task.row_index,
                    task.language,
                    filename,
                    final_filename,
                )
            language_audio_files[task.language][final_filename] = audio_bytes

            summary.language_tasks_succeeded += 1
            summary.language_tasks_failed = max(0, summary.language_tasks_failed - 1)

            pending_failures = row_unresolved_failures.get(task.row_index, 0) - 1
            row_unresolved_failures[task.row_index] = max(0, pending_failures)
            if row_unresolved_failures[task.row_index] == 0 and task.row_index in recoverable_failed_rows:
                recoverable_failed_rows.remove(task.row_index)
                summary.rows_failed = max(0, summary.rows_failed - 1)
                summary.rows_succeeded += 1

            logger.info(
                "Job %s | row %d | lang %s: retry succeeded; restored file %s",
                job_id,
                task.row_index,
                task.language,
                final_filename,
            )
        except Exception as exc:
            logger.error(
                "Job %s | row %d | lang %s: retry failed; keeping task as failed (%s)",
                job_id,
                task.row_index,
                task.language,
                exc,
            )
        finally:
            await jobs_store.update_summary(job_id, summary)


async def run_excel_batch_job(
    *,
    job_id: str,
    excel_path: str,
    target_languages: list[str],
    max_language_parallelism: int | None = None,
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
            max_language_parallelism,
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
    max_language_parallelism: int | None,
    jobs_store: JobsStore,
    summary: JobSummary,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    summary.started_at = await jobs_store.start(job_id)
    logger.info("Job %s started | excel=%s | languages=%s", job_id, excel_path, target_languages)

    try:
        upload_to_s3 = _should_upload_to_s3(runtime_config=runtime_config)
        _validate_english_voice_if_needed(target_languages, runtime_config=runtime_config)
        _validate_qc_is_enabled(runtime_config=runtime_config)
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

    current_activity_name: str | None = None
    activity_upload_counts: dict[str, int] = {}
    language_audio_files: dict[str, dict[str, bytes]] | None = (
        _new_language_audio_buffers(target_languages) if s3_client else None
    )
    current_activity_failed_tasks: list[FailedLanguageTask] | None = [] if s3_client else None
    row_unresolved_failures: dict[int, int] = {}
    recoverable_failed_rows: set[int] = set()
    translation_parallelism = _resolve_language_parallelism(
        total_languages=len(target_languages),
        requested_parallelism=max_language_parallelism,
        runtime_config=runtime_config,
    )

    try:
        for row in rows:
            row_ok = True
            row_had_task_failure = False
            try:
                row_activity_name = _resolve_activity_segment_name(row.activity_name, current_activity_name)
                if s3_client is not None and language_audio_files is not None:
                    if current_activity_name is None:
                        current_activity_name = row_activity_name
                    elif row_activity_name != current_activity_name:
                        if current_activity_failed_tasks is not None:
                            await _retry_failed_activity_tasks(
                                job_id=job_id,
                                activity_name=current_activity_name,
                                failed_tasks=current_activity_failed_tasks,
                                language_audio_files=language_audio_files,
                                summary=summary,
                                jobs_store=jobs_store,
                                row_unresolved_failures=row_unresolved_failures,
                                recoverable_failed_rows=recoverable_failed_rows,
                                runtime_config=runtime_config,
                            )
                        await _upload_activity_archives(
                            job_id=job_id,
                            activity_name=current_activity_name,
                            target_languages=target_languages,
                            language_audio_files=language_audio_files,
                            s3_client=s3_client,
                            jobs_store=jobs_store,
                            summary=summary,
                            activity_upload_counts=activity_upload_counts,
                        )
                        language_audio_files = _new_language_audio_buffers(target_languages)
                        current_activity_failed_tasks = [] if current_activity_failed_tasks is not None else None
                        current_activity_name = row_activity_name

                logger.info(
                    "Job %s | row %d: translating into %d languages (parallelism=%d)",
                    job_id,
                    row.row_index,
                    len(target_languages),
                    translation_parallelism,
                )
                translation_raw_results = await _translate_row_languages(
                    text=row.text,
                    target_languages=target_languages,
                    max_parallelism=translation_parallelism,
                    runtime_config=runtime_config,
                )
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

                translations_to_qc: dict[str, str] = {}
                for language, translated_text, translation_error in translation_results:
                    effective_text = (translated_text or "").strip()
                    if translation_error is not None or not effective_text:
                        failure_reason = translation_error or "empty translation result"
                        summary.translation_fallbacks += 1
                        summary.language_tasks_failed += 1
                        row_had_task_failure = True
                        row_unresolved_failures[row.row_index] = row_unresolved_failures.get(row.row_index, 0) + 1
                        if current_activity_failed_tasks is not None:
                            current_activity_failed_tasks.append(
                                FailedLanguageTask(
                                    row_index=row.row_index,
                                    row_text=row.text,
                                    emotion=row.emotion,
                                    activity_name=row.activity_name,
                                    audio_type=row.audio_type,
                                    language=language,
                                    reason=failure_reason,
                                )
                            )
                        logger.error(
                            "Job %s | row %d | lang %s: translation failed; skipping TTS (%s)",
                            job_id,
                            row.row_index,
                            language,
                            failure_reason,
                        )
                        await jobs_store.update_summary(job_id, summary)
                        continue
                    translations_to_qc[language] = effective_text

                qc_output_by_language: dict[str, str] = {}
                qc_failures_by_language: dict[str, str] = {}
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
                        for language, translated_text in translations_to_qc.items():
                            qc_text = (qc_results.get(language) or "").strip()
                            if not qc_text:
                                qc_failures_by_language[language] = "QC returned empty translation"
                            else:
                                qc_output_by_language[language] = qc_text
                        logger.info("Job %s | row %d: QC complete", job_id, row.row_index)
                    except QCError as exc:
                        reason = f"QC failed: {exc}"
                        logger.error("Job %s | row %d: %s", job_id, row.row_index, reason)
                        for language in translations_to_qc:
                            qc_failures_by_language[language] = reason
                    except Exception as exc:
                        reason = f"Unexpected QC failure: {exc}"
                        logger.error("Job %s | row %d: %s", job_id, row.row_index, reason)
                        for language in translations_to_qc:
                            qc_failures_by_language[language] = reason

                for language in target_languages:
                    effective_text = qc_output_by_language.get(language, "")
                    if not effective_text:
                        if language in qc_failures_by_language:
                            failure_reason = qc_failures_by_language[language]
                            summary.language_tasks_failed += 1
                            row_had_task_failure = True
                            row_unresolved_failures[row.row_index] = row_unresolved_failures.get(row.row_index, 0) + 1
                            if current_activity_failed_tasks is not None:
                                current_activity_failed_tasks.append(
                                    FailedLanguageTask(
                                        row_index=row.row_index,
                                        row_text=row.text,
                                        emotion=row.emotion,
                                        activity_name=row.activity_name,
                                        audio_type=row.audio_type,
                                        language=language,
                                        reason=failure_reason,
                                    )
                                )
                            logger.error(
                                "Job %s | row %d | lang %s: skipping TTS because QC did not produce output (%s)",
                                job_id,
                                row.row_index,
                                language,
                                failure_reason,
                            )
                            await jobs_store.update_summary(job_id, summary)
                        continue

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
                        failure_reason = str(exc)
                        summary.language_tasks_failed += 1
                        row_had_task_failure = True
                        row_unresolved_failures[row.row_index] = row_unresolved_failures.get(row.row_index, 0) + 1
                        if current_activity_failed_tasks is not None:
                            current_activity_failed_tasks.append(
                                FailedLanguageTask(
                                    row_index=row.row_index,
                                    row_text=row.text,
                                    emotion=row.emotion,
                                    activity_name=row.activity_name,
                                    audio_type=row.audio_type,
                                    language=language,
                                    reason=failure_reason,
                                )
                            )
                        logger.error(
                            "Job %s | row %d | lang %s: TTS failed; skipping audio output (%s)",
                            job_id,
                            row.row_index,
                            language,
                            failure_reason,
                        )
                        await jobs_store.update_summary(job_id, summary)
                        continue

                    if s3_client is not None:
                        audio_bytes = compress_mp3_bytes(audio_bytes)

                    filename = _build_output_filename(
                        audio_type=row.audio_type,
                        row_index=row.row_index,
                        language=language,
                    )

                    if s3_client is None:
                        summary.language_tasks_succeeded += 1
                        logger.info(
                            "Job %s | row %d | lang %s: done (discarded audio; no upload)",
                            job_id,
                            row.row_index,
                            language,
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
                            "Job %s | row %d | lang %s: ready for zip -> %s",
                            job_id,
                            row.row_index,
                            language,
                            final_name_for_log,
                        )
                    await jobs_store.update_summary(job_id, summary)
            except Exception as exc:
                row_ok = False
                summary.unexpected_row_errors += 1
                logger.exception("Job %s | row %d: unexpected row failure: %s", job_id, row.row_index, exc)
            finally:
                summary.rows_processed += 1
                if row_ok and not row_had_task_failure:
                    summary.rows_succeeded += 1
                else:
                    summary.rows_failed += 1
                    if row_ok and row_had_task_failure:
                        recoverable_failed_rows.add(row.row_index)

                logger.info(
                    "Job %s | row %d complete | tasks succeeded=%d failed=%d",
                    job_id,
                    row.row_index,
                    summary.language_tasks_succeeded,
                    summary.language_tasks_failed,
                )
                await jobs_store.update_summary(job_id, summary)

        if s3_client is not None and language_audio_files is not None and current_activity_name is not None:
            if current_activity_failed_tasks is not None:
                await _retry_failed_activity_tasks(
                    job_id=job_id,
                    activity_name=current_activity_name,
                    failed_tasks=current_activity_failed_tasks,
                    language_audio_files=language_audio_files,
                    summary=summary,
                    jobs_store=jobs_store,
                    row_unresolved_failures=row_unresolved_failures,
                    recoverable_failed_rows=recoverable_failed_rows,
                    runtime_config=runtime_config,
                )
            await _upload_activity_archives(
                job_id=job_id,
                activity_name=current_activity_name,
                target_languages=target_languages,
                language_audio_files=language_audio_files,
                s3_client=s3_client,
                jobs_store=jobs_store,
                summary=summary,
                activity_upload_counts=activity_upload_counts,
            )

        await jobs_store.complete(job_id, summary)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch execution failed: {exc}", summary)
