from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from services.email import send_batch_summary_email
from services.runtime_config import RuntimeConfig, get_config_value
from services.video_pipeline.batch_excel import HeyGenBatchRow
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.heygen_client import (
    clear_talking_photos,
    get_heygen_api_key,
    upload_talking_photo,
)
from services.video_pipeline.pipeline import _guess_image_content_type, run_video_job
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec

logger = logging.getLogger(__name__)


def _local_video_url(video_path: str, output_base_dir: Path) -> str | None:
    path = Path(video_path)
    try:
        rel = path.resolve().relative_to(output_base_dir.resolve())
    except ValueError:
        return None
    if not path.exists():
        return None
    return f"/output/{rel.as_posix()}"


_DEFAULT_BATCH_CONCURRENCY = 4


def _batch_concurrency(runtime_config: RuntimeConfig | None, row_count: int) -> int:
    """Max renders in flight at once. Override with HEYGEN_BATCH_CONCURRENCY;
    clamped to [1, row_count] so we never spawn more workers than there are rows."""
    raw = get_config_value("HEYGEN_BATCH_CONCURRENCY", runtime_config=runtime_config)
    try:
        value = int(raw) if raw else _DEFAULT_BATCH_CONCURRENCY
    except (TypeError, ValueError):
        value = _DEFAULT_BATCH_CONCURRENCY
    return max(1, min(value, max(1, row_count)))


def _email_config(runtime_config: RuntimeConfig | None) -> tuple[str, str, list[str]] | None:
    api_key = get_config_value("RESEND_API_KEY", runtime_config=runtime_config)
    from_addr = get_config_value("RESEND_FROM_ADDRESS", runtime_config=runtime_config)
    to_raw = get_config_value("NOTIFY_EMAILS", runtime_config=runtime_config)
    if not (api_key and from_addr and to_raw):
        return None
    recipients = [a.strip() for a in to_raw.split(",") if a.strip()]
    return (api_key, from_addr, recipients) if recipients else None


async def run_video_batch_job(
    *,
    batch_id: str,
    rows: list[HeyGenBatchRow],
    image_bytes: bytes,
    image_filename: str,
    character: str = "indian",
    video_prompt: str | None,
    motion_prompt: str | None,
    publish_date: str | None,
    output_dir: Path,
    output_base_dir: Path,
    batch_store: VideoBatchJobsStore,
    video_jobs_store: VideoJobsStore,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    await batch_store.start(batch_id)
    failed_rows: list[dict] = []

    # Every row in a batch uses the SAME image, so upload the talking photo once
    # up front and reuse its id for every row. Otherwise each row re-uploads the
    # identical image via /v1/talking_photo and, past HeyGen's 3-avatar cap,
    # triggers list→delete→re-upload churn on every video — pure serial overhead.
    shared_talking_photo_id: str | None = None
    try:
        heygen_key = get_heygen_api_key(runtime_config=runtime_config)
        # Free all 3 photo-avatar slots before uploading so the shared upload never
        # trips HeyGen's cap on leftover photos from a prior run. Runs are sequential
        # (one batch/generation at a time), so nothing else is rendering against these.
        try:
            await asyncio.to_thread(clear_talking_photos, api_key=heygen_key)
        except Exception as exc:
            logger.warning("Batch %s | pre-upload slot clear failed (continuing): %s", batch_id, exc)
        shared_talking_photo_id = await asyncio.to_thread(
            upload_talking_photo,
            api_key=heygen_key,
            content=image_bytes,
            content_type=_guess_image_content_type(image_filename),
        )
        logger.info("Batch %s | uploaded shared talking photo %s", batch_id, shared_talking_photo_id)
    except Exception as exc:
        # Non-fatal: fall back to per-row upload inside run_video_job.
        logger.warning("Batch %s | shared talking photo upload failed, rows will upload individually: %s", batch_id, exc)

    # HeyGen renders are asynchronous: each row submits a render and then spends
    # most of its wall-clock just polling status. Running rows concurrently means
    # those polls overlap, so batch time approaches the slowest single render
    # instead of the sum of all of them. A semaphore caps in-flight renders so we
    # don't trip HeyGen rate limits / concurrent-render quotas.
    concurrency = _batch_concurrency(runtime_config, len(rows))
    semaphore = asyncio.Semaphore(concurrency)
    logger.info("Batch %s | running %d rows with concurrency %d", batch_id, len(rows), concurrency)

    async def _run_one_row(row: HeyGenBatchRow) -> None:
        async with semaphore:
            job_id = uuid.uuid4().hex
            await video_jobs_store.create(job_id)
            await batch_store.update_row(batch_id, row.row_index, job_id=job_id, status="running")

            spec = VideoJobSpec(
                script=row.script,
                character=character or "indian",
                video_title=row.video_title,
                video_prompt=video_prompt or None,
                motion_prompt=motion_prompt or None,
                publish_date=publish_date or None,
                talking_photo_id=shared_talking_photo_id,
            )

            try:
                await run_video_job(
                    job_id=job_id,
                    spec=spec,
                    image_bytes=image_bytes,
                    image_filename=image_filename,
                    output_dir=output_dir,
                    jobs_store=video_jobs_store,
                    runtime_config=runtime_config,
                )

                video_state = await video_jobs_store.get(job_id)
                if not video_state or video_state.status != "completed":
                    raise RuntimeError(
                        (video_state.error if video_state else None) or "video job did not complete"
                    )

                video_local_url = _local_video_url(video_state.summary.video_path or "", output_base_dir)
                await batch_store.update_row(
                    batch_id, row.row_index,
                    status="completed",
                    video_local_url=video_local_url,
                    nas_path=video_state.summary.nas_path,
                )
                await batch_store.row_succeeded(batch_id)
                logger.info("Batch %s | row %d completed (job %s)", batch_id, row.row_index, job_id)

            except Exception as exc:
                logger.error("Batch %s | row %d failed: %s", batch_id, row.row_index, exc)
                await batch_store.update_row(
                    batch_id, row.row_index,
                    status="failed",
                    error=str(exc),
                )
                await batch_store.row_failed(batch_id)
                failed_rows.append({
                    "row_index": row.row_index,
                    "video_title": row.video_title,
                    "error": str(exc),
                })

    # return_exceptions=True so one row's unexpected failure can't cancel the rest;
    # _run_one_row already records per-row failures, so this is just a safety net.
    await asyncio.gather(*(_run_one_row(row) for row in rows), return_exceptions=True)

    await batch_store.complete(batch_id)

    cfg = _email_config(runtime_config)
    if cfg:
        batch_state = await batch_store.get(batch_id)
        try:
            await asyncio.to_thread(
                send_batch_summary_email,
                total=batch_state.total if batch_state else len(rows),
                succeeded=batch_state.done if batch_state else 0,
                failed=batch_state.failed_count if batch_state else len(failed_rows),
                failed_rows=failed_rows,
                resend_api_key=cfg[0],
                from_address=cfg[1],
                to_addresses=cfg[2],
            )
        except Exception as exc:
            logger.warning("Batch %s: email notification failed: %s", batch_id, exc)
    else:
        logger.info("Batch %s: email skipped (RESEND_API_KEY / NOTIFY_EMAILS not configured)", batch_id)
