from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from services.email import send_batch_summary_email
from services.runtime_config import RuntimeConfig, get_config_value
from services.video_pipeline.batch_excel import HeyGenBatchRow
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.pipeline import run_video_job
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
    video_prompt: str | None,
    motion_prompt: str | None,
    output_dir: Path,
    output_base_dir: Path,
    batch_store: VideoBatchJobsStore,
    video_jobs_store: VideoJobsStore,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    await batch_store.start(batch_id)
    failed_rows: list[dict] = []

    for row in rows:
        job_id = uuid.uuid4().hex
        await video_jobs_store.create(job_id)
        await batch_store.update_row(batch_id, row.row_index, job_id=job_id, status="running")

        spec = VideoJobSpec(
            script=row.script,
            video_title=row.video_title,
            video_prompt=video_prompt or None,
            motion_prompt=motion_prompt or None,
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
