from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from services.email import EmailSettings, send_batch_summary_email
from services.nas import NasConfig
from services.video_pipeline.batch_excel import HeyGenBatchRow
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.pipeline import run_video_job
from services.video_pipeline.renderer import VideoRenderer
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.heygen_client import HeyGenSettings
from services.video_pipeline.speech import SpeechSynth
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


def _batch_concurrency(configured: int, row_count: int) -> int:
    """Max renders in flight at once, clamped to [1, row_count] so we never
    spawn more workers than there are rows."""
    return max(1, min(configured, max(1, row_count)))


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
    renderer: VideoRenderer,
    speech: SpeechSynth,
    slots: TalkingPhotoSlots,
    heygen: HeyGenSettings,
    nas_config: NasConfig,
    email: EmailSettings,
) -> None:
    await batch_store.start(batch_id)
    failed_rows: list[dict] = []

    # Every row in a batch uses the SAME image, so acquire the talking photo once
    # up front and reuse its id for every row. Otherwise each row re-uploads the
    # identical image and, past the provider's photo cap, triggers
    # list→delete→re-upload churn on every video — pure serial overhead.
    shared_talking_photo_id: str | None = None
    try:
        shared_talking_photo_id = await slots.acquire(
            image_bytes=image_bytes,
            image_filename=image_filename,
            label=f"Batch {batch_id}",
        )
    except Exception as exc:
        # Non-fatal: fall back to per-row acquire inside run_video_job.
        logger.warning("Batch %s | shared talking photo upload failed, rows will upload individually: %s", batch_id, exc)

    # HeyGen renders are asynchronous: each row submits a render and then spends
    # most of its wall-clock just polling status. Running rows concurrently means
    # those polls overlap, so batch time approaches the slowest single render
    # instead of the sum of all of them. A semaphore caps in-flight renders so we
    # don't trip HeyGen rate limits / concurrent-render quotas.
    concurrency = _batch_concurrency(heygen.batch_concurrency, len(rows))
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
                    renderer=renderer,
                    speech=speech,
                    slots=slots,
                    nas_config=nas_config,
                    voice_id=heygen.voice_for_character(character),
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

    if email.configured:
        batch_state = await batch_store.get(batch_id)
        try:
            await asyncio.to_thread(
                send_batch_summary_email,
                total=batch_state.total if batch_state else len(rows),
                succeeded=batch_state.done if batch_state else 0,
                failed=batch_state.failed_count if batch_state else len(failed_rows),
                failed_rows=failed_rows,
                resend_api_key=email.api_key,
                from_address=email.from_address,
                to_addresses=list(email.to_addresses),
            )
        except Exception as exc:
            logger.warning("Batch %s: email notification failed: %s", batch_id, exc)
    else:
        logger.info("Batch %s: email skipped (RESEND_API_KEY / NOTIFY_EMAILS not configured)", batch_id)
