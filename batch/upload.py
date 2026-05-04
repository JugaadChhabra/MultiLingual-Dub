from __future__ import annotations

import asyncio
import logging

from batch.models import JobSummary
from batch.naming import _next_activity_folder_name
from batch.store import JobsStore
from services.languages import LANGUAGE_NAMES
from services.s3 import S3Client

logger = logging.getLogger(__name__)


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
