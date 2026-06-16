from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
import zipfile

from batch.models import ArchiveDownload, JobSummary
from batch.naming import _next_activity_folder_name
from batch.store import JobsStore
from services.languages import LANGUAGE_NAMES
from services.s3 import S3Client

logger = logging.getLogger(__name__)


def _safe_archive_name(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "archive"


def _build_zip_bytes(audio_files: dict[str, bytes]) -> bytes:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, audio_bytes in audio_files.items():
            zf.writestr(filename, audio_bytes)
    return zip_buffer.getvalue()


def _write_local_archive(
    *,
    output_dir: Path,
    job_id: str,
    activity_name: str,
    language_label: str,
    audio_files: dict[str, bytes],
    reason: str,
    error: str | None = None,
) -> ArchiveDownload:
    safe_activity = _safe_archive_name(activity_name)
    safe_language = _safe_archive_name(language_label)
    archive_dir = output_dir / "batch_archives" / job_id / safe_activity
    archive_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{safe_activity}-{safe_language}.zip"
    archive_path = archive_dir / filename
    archive_path.write_bytes(_build_zip_bytes(audio_files))
    rel_path = archive_path.relative_to(output_dir).as_posix()
    return ArchiveDownload(
        activity_name=activity_name,
        language=language_label,
        filename=filename,
        path=str(archive_path),
        url=f"/output/{rel_path}",
        reason=reason,
        error=error,
    )


def _record_local_archive(
    *,
    output_dir: Path | None,
    job_id: str,
    activity_name: str,
    language_label: str,
    audio_files: dict[str, bytes],
    reason: str,
    summary: JobSummary,
    error: str | None = None,
) -> None:
    if output_dir is None:
        summary.local_archives_failed += 1
        logger.error(
            "Job %s | activity %s | lang %s: local archive fallback unavailable (%s)",
            job_id,
            activity_name,
            language_label,
            reason,
        )
        return

    try:
        archive = _write_local_archive(
            output_dir=output_dir,
            job_id=job_id,
            activity_name=activity_name,
            language_label=language_label,
            audio_files=audio_files,
            reason=reason,
            error=error,
        )
        summary.local_archives_succeeded += 1
        summary.archive_downloads.append(archive)
        logger.info(
            "Job %s | activity %s | lang %s: local fallback archive ready -> %s",
            job_id,
            activity_name,
            language_label,
            archive.path,
        )
    except Exception as exc:
        summary.local_archives_failed += 1
        logger.error(
            "Job %s | activity %s | lang %s: local archive fallback failed: %s",
            job_id,
            activity_name,
            language_label,
            exc,
        )


async def _upload_activity_archives(
    *,
    job_id: str,
    activity_name: str,
    target_languages: list[str],
    language_audio_files: dict[str, dict[str, bytes]],
    s3_client: S3Client | None,
    jobs_store: JobsStore,
    summary: JobSummary,
    activity_upload_counts: dict[str, int],
    output_dir: Path | None = None,
    append_mode: bool = False,
) -> None:
    if append_mode:
        folder_name = activity_name
    else:
        folder_name = _next_activity_folder_name(activity_name, activity_upload_counts)
    logger.info(
        "Job %s | activity %s: archiving zip files for %d languages under %s",
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
            if s3_client is None:
                summary.uploads_skipped += 1
                if summary.upload_warning is None:
                    fallback_detail = (
                        "Local ZIP downloads were prepared instead."
                        if output_dir is not None
                        else "No local output directory was configured for fallback downloads."
                    )
                    summary.upload_warning = (
                        "Cloud upload is disabled because BATCH_ENABLE_S3_UPLOAD is not true. "
                        f"{fallback_detail}"
                    )
                _record_local_archive(
                    output_dir=output_dir,
                    job_id=job_id,
                    activity_name=folder_name,
                    language_label=language_label,
                    audio_files=audio_files,
                    reason="s3_disabled",
                    summary=summary,
                )
                logger.warning(
                    "Job %s | activity %s | lang %s: S3 upload skipped; local fallback requested",
                    job_id,
                    activity_name,
                    language_label,
                )
                continue

            if append_mode:
                result = await asyncio.to_thread(
                    s3_client.append_to_language_zip,
                    language_label,
                    audio_files,
                    folder_name,
                )
                summary.uploads_succeeded += 1
                logger.info(
                    "Job %s | activity %s | lang %s: appended %s file(s) into zip (overwrote %s, total now %s) -> %s",
                    job_id,
                    activity_name,
                    language_label,
                    result.get("added_files", len(audio_files)),
                    result.get("overwritten_files", "0"),
                    result.get("total_files", "?"),
                    result["key"],
                )
            else:
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
            if summary.upload_warning is None:
                summary.upload_warning = (
                    "Cloud upload failed for at least one ZIP. "
                    "Local ZIP downloads were prepared for failed uploads."
                )
            _record_local_archive(
                output_dir=output_dir,
                job_id=job_id,
                activity_name=folder_name,
                language_label=LANGUAGE_NAMES.get(language, language),
                audio_files=audio_files,
                reason="s3_failed",
                summary=summary,
                error=str(exc),
            )
            logger.error(
                "Job %s | activity %s | lang %s: zip upload failed: %s",
                job_id,
                activity_name,
                language,
                exc,
            )
        finally:
            await jobs_store.update_summary(job_id, summary)
