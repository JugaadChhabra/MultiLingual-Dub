"""Driving an Excel sheet through the audio pipeline.

What is left here is orchestration and nothing else: walk the rows, notice where
one activity ends and the next begins, and at each boundary retry what failed
and upload what succeeded. Producing audio for a row lives in batch.voiceover,
naming and holding it in batch.activity, and counting it in batch.tally.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path

from batch.activity import ActivityBuffer
from batch.excel import read_excel_rows
from batch.models import ExcelRow
from batch.naming import _resolve_activity_segment_name
from batch.store import JobsStore
from batch.tally import BatchTally
from batch.upload import _upload_activity_archives
from batch.voiceover import RowOutcome, TaskFailure, VoiceoverDeps, voice_row
from services.elevenlabs import ElevenLabsSettings
from services.languages import LANGUAGE_NAMES
from services.s3 import S3Client
from services.settings import Settings

logger = logging.getLogger(__name__)


def english_voice_is_required(target_languages: list[str], settings: ElevenLabsSettings) -> None:
    """Whether ENGLISH_VOICE is needed depends on the request, not the config,
    so this is checked at the route where the languages are known — not inside
    the running job."""
    has_english_target = any(language.strip().lower().startswith("en") for language in target_languages)
    if has_english_target and not settings.english_voice_id:
        raise ValueError("ENGLISH_VOICE is required when generating English batch audio")


def _resolve_language_parallelism(
    *,
    total_languages: int,
    requested_parallelism: int | None,
    configured_parallelism: int | None,
) -> int:
    requested = requested_parallelism
    if requested is None:
        requested = configured_parallelism
    if requested is None:
        requested = total_languages
    requested = max(1, requested)
    return min(total_languages, requested)


async def _retry_failed_tasks(
    *,
    job_id: str,
    activity_name: str,
    failures: dict[int, list[TaskFailure]],
    rows_by_index: dict[int, ExcelRow],
    buffer: ActivityBuffer,
    tally: BatchTally,
    deps: VoiceoverDeps,
) -> None:
    """Re-attempt an activity's failed language tasks, once, before upload.

    Deferred to the activity boundary rather than retried inline: a provider
    that rate-limited us on row 3 is far more likely to have recovered by the
    end of the activity than a few milliseconds later.

    Failures are regrouped BY ROW so each retried row is one call into
    voice_row — the same code the main pass uses, with the same single batched
    QC call. Retrying six tasks spread over two rows costs two QC calls, not six.
    """
    if not failures:
        return

    total = sum(len(v) for v in failures.values())
    logger.info(
        "Job %s | activity %s: retrying %d failed language task(s) before upload",
        job_id, activity_name, total,
    )

    for row_index, row_failures in failures.items():
        row = rows_by_index[row_index]
        languages = [failure.language for failure in row_failures]
        prefix = f"Job {job_id} | row {row_index} | retry | "
        try:
            outcome = await voice_row(row, languages, deps, log_prefix=prefix)
        except Exception as exc:
            logger.error("%sretry crashed, keeping tasks as failed: %s", prefix, exc)
            continue

        for language, audio in outcome.audio.items():
            buffer.add(row, language, audio, log_prefix=prefix)
            tally.retry_succeeded(row_index)
            logger.info("%slang %s: retry succeeded", prefix, language)

        for failure in outcome.failures:
            logger.error(
                "%slang %s: retry failed; keeping task as failed (%s)",
                prefix, failure.language, failure.reason,
            )


async def run_excel_batch_job(
    *,
    job_id: str,
    excel_path: str,
    target_languages: list[str],
    max_language_parallelism: int | None = None,
    jobs_store: JobsStore,
    settings: Settings,
    teaching_mode: bool = False,
    output_dir: Path | None = None,
    mode: str = "create",
) -> None:
    """
    Run a batch job to completion without a fixed global timeout.
    Long-running jobs remain active until they finish or hit a terminal error.

    mode="create" (default): generate audio and upload fresh language zips per activity.
    mode="append": require that each activity name from the Excel already exists as a
    folder on S3; merge the newly generated audio files into the existing language zips
    (new files overwrite same-named entries).
    """
    tally = BatchTally()
    try:
        await _run_batch_job_impl(
            job_id=job_id,
            excel_path=excel_path,
            target_languages=target_languages,
            max_language_parallelism=max_language_parallelism,
            jobs_store=jobs_store,
            tally=tally,
            settings=settings,
            teaching_mode=teaching_mode,
            output_dir=output_dir,
            mode=mode,
        )
    except Exception as exc:
        logger.exception("Job %s crashed unexpectedly: %s", job_id, exc)
        await jobs_store.fail(job_id, f"Batch execution crashed: {exc}", tally.summary, exc=exc)


async def _run_batch_job_impl(
    *,
    job_id: str,
    excel_path: str,
    target_languages: list[str],
    max_language_parallelism: int | None,
    jobs_store: JobsStore,
    tally: BatchTally,
    settings: Settings,
    teaching_mode: bool = False,
    output_dir: Path | None = None,
    mode: str = "create",
) -> None:
    append_mode = mode == "append"
    tally.started(await jobs_store.start(job_id))
    logger.info("Job %s started | excel=%s | languages=%s", job_id, excel_path, target_languages)

    try:
        s3_client: S3Client | None = None
        if settings.batch.upload_to_s3:
            s3_client = S3Client(settings.s3)
        else:
            tally.warn_about_uploads(
                "Cloud upload is disabled because BATCH_ENABLE_S3_UPLOAD is not true. "
                "Local ZIP downloads will be prepared instead."
            )

        try:
            rows = await asyncio.to_thread(read_excel_rows, excel_path)
        finally:
            excel_file = Path(excel_path)
            if excel_file.exists():
                try:
                    excel_file.unlink()
                except OSError as exc:
                    logger.warning(
                        "Job %s: failed to delete temp excel file %s: %s", job_id, excel_path, exc
                    )

        tally.set_totals(rows=len(rows), languages=len(target_languages))
        await jobs_store.update_summary(job_id, tally.summary)
        logger.info(
            "Job %s | %d rows x %d languages = %d tasks",
            job_id, len(rows), len(target_languages), len(rows) * len(target_languages),
        )

        existing_files_by_folder = await _preload_append_targets(
            job_id=job_id,
            rows=rows,
            target_languages=target_languages,
            s3_client=s3_client,
            append_mode=append_mode,
        )
    # No S3ConfigError branch: S3 credentials are resolved at the route now, so
    # a misconfigured bucket rejects the upload rather than failing a started job.
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch setup failed: {exc}", tally.summary, exc=exc)
        return

    if not rows:
        await jobs_store.complete(job_id, tally.summary)
        return

    deps = VoiceoverDeps(
        sarvam=settings.sarvam,
        qc=settings.qc,
        eleven=settings.eleven,
        teaching_mode=teaching_mode,
        translation_parallelism=_resolve_language_parallelism(
            total_languages=len(target_languages),
            requested_parallelism=max_language_parallelism,
            configured_parallelism=settings.batch.max_language_parallelism,
        ),
    )

    rows_by_index = {row.row_index: row for row in rows}
    activity_upload_counts: dict[str, int] = {}
    current_activity: str | None = None
    buffer = ActivityBuffer(target_languages)
    activity_failures: dict[int, list[TaskFailure]] = defaultdict(list)

    async def _close_activity(name: str) -> None:
        """Retry what failed, upload what stuck, then start fresh."""
        nonlocal buffer, activity_failures
        await _retry_failed_tasks(
            job_id=job_id,
            activity_name=name,
            failures=dict(activity_failures),
            rows_by_index=rows_by_index,
            buffer=buffer,
            tally=tally,
            deps=deps,
        )
        tally.collisions_resolved(buffer.collisions_resolved)
        await _upload_activity_archives(
            job_id=job_id,
            activity_name=name,
            target_languages=target_languages,
            buffer=buffer,
            s3_client=s3_client,
            tally=tally,
            activity_upload_counts=activity_upload_counts,
            output_dir=output_dir,
            append_mode=append_mode,
        )
        await jobs_store.update_summary(job_id, tally.summary)
        buffer = ActivityBuffer(target_languages)
        activity_failures = defaultdict(list)

    try:
        for row in rows:
            if await jobs_store.is_cancelled(job_id):
                await jobs_store.cancel(job_id, tally.summary)
                return

            try:
                activity = _resolve_activity_segment_name(row.activity_name, current_activity)
                if current_activity is None:
                    current_activity = activity
                elif activity != current_activity:
                    await _close_activity(current_activity)
                    current_activity = activity

                languages = _languages_still_needed(
                    job_id=job_id,
                    row=row,
                    target_languages=target_languages,
                    buffer=buffer,
                    tally=tally,
                    existing=existing_files_by_folder.get(current_activity, {}),
                    append_mode=append_mode,
                )
                if not languages:
                    continue

                prefix = f"Job {job_id} | row {row.row_index} | "
                outcome = await voice_row(row, languages, deps, log_prefix=prefix)

                for language, audio in outcome.audio.items():
                    buffer.add(row, language, audio, log_prefix=prefix)
                for failure in outcome.failures:
                    activity_failures[row.row_index].append(failure)

                tally.row_finished(row.row_index, outcome)
                logger.info(
                    "%scomplete | languages ok=%d failed=%d",
                    prefix, len(outcome.audio), len(outcome.failures),
                )
            except Exception as exc:
                tally.row_crashed(row.row_index)
                logger.exception("Job %s | row %d: unexpected row failure: %s", job_id, row.row_index, exc)
            finally:
                # One write per row: the UI's progress bar is rows_processed /
                # total_rows, so this is the finest granularity anyone can see.
                await jobs_store.update_summary(job_id, tally.summary)

        if current_activity is not None:
            await _close_activity(current_activity)

        await jobs_store.complete(job_id, tally.summary)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Batch execution failed: {exc}", tally.summary, exc=exc)


def _languages_still_needed(
    *,
    job_id: str,
    row: ExcelRow,
    target_languages: list[str],
    buffer: ActivityBuffer,
    tally: BatchTally,
    existing: dict[str, set[str]],
    append_mode: bool,
) -> list[str]:
    """Which of a row's languages actually need generating.

    In create mode, all of them. In append mode, only those whose file is not
    already sitting in the activity's remote zip — regenerating those would burn
    provider credits to produce a byte-identical replacement.
    """
    if not append_mode:
        return list(target_languages)

    skipped: list[str] = []
    needed: list[str] = []
    for language in target_languages:
        expected = buffer.expected_filename(row, language)
        if expected in existing.get(language, set()):
            skipped.append(language)
        else:
            needed.append(language)

    if skipped:
        tally.row_skipped_languages(len(skipped))
        logger.info(
            "Job %s | row %d: append mode skipping %d/%d languages already present in zip (%s)",
            job_id, row.row_index, len(skipped), len(target_languages), ", ".join(skipped),
        )
    if not needed:
        logger.info(
            "Job %s | row %d: append mode skipping entire row — all languages already present",
            job_id, row.row_index,
        )
    return needed


async def _preload_append_targets(
    *,
    job_id: str,
    rows: list[ExcelRow],
    target_languages: list[str],
    s3_client: S3Client | None,
    append_mode: bool,
) -> dict[str, dict[str, set[str]]]:
    """For append mode, confirm every activity folder exists on S3 and read back
    what each language zip already contains. Raises if a folder is missing —
    appending to a folder that isn't there would silently create a new one."""
    if not append_mode:
        return {}
    if s3_client is None:
        raise ValueError("Append mode requires S3 upload to be enabled (BATCH_ENABLE_S3_UPLOAD=true).")

    unique_folders: list[str] = []
    seen: set[str] = set()
    current: str | None = None
    for row in rows:
        segment = _resolve_activity_segment_name(row.activity_name, current)
        current = segment
        if segment not in seen:
            seen.add(segment)
            unique_folders.append(segment)

    missing = [
        folder for folder in unique_folders
        if not await asyncio.to_thread(s3_client.folder_exists, folder)
    ]
    if missing:
        raise ValueError("Append mode: the following S3 folder(s) do not exist: " + ", ".join(missing))
    logger.info(
        "Job %s | append mode | validated %d folder(s) exist on S3: %s",
        job_id, len(unique_folders), ", ".join(unique_folders),
    )

    existing_files_by_folder: dict[str, dict[str, set[str]]] = {}
    for folder in unique_folders:
        per_lang: dict[str, set[str]] = {}
        for language in target_languages:
            label = LANGUAGE_NAMES.get(language, language)
            per_lang[language] = await asyncio.to_thread(
                s3_client.list_zip_filenames, folder, label
            )
        existing_files_by_folder[folder] = per_lang
        logger.info(
            "Job %s | append mode | preloaded existing entries for %s: %s",
            job_id, folder,
            ", ".join(f"{LANGUAGE_NAMES.get(l, l)}={len(per_lang[l])}" for l in target_languages),
        )
    return existing_files_by_folder
