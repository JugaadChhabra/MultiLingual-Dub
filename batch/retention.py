"""Retention: finished audio jobs are removed once they are old enough.

A sweep removes every audio job (Excel or story) that ended — completed,
failed or cancelled — more than the retention window ago
(``OUTPUT_RETENTION_DAYS``, default 14): its local fallback zips under
``<output>/batch_archives/<job_id>/`` and its job record, in memory and on disk.

It never touches anything else: not a queued or running job, not a terminal
job with no finish time, and nothing outside ``batch_archives/<job_id>`` and
the audio jobs store. Everything the video pipeline keeps under the output
folder (``heygen/``: its job store, renders, library cache, script history) is
out of reach by construction.

The app runs ``retention_loop`` from start-up: one sweep at once, then one
every six hours. A sweep that fails is logged and the next one still runs.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from batch.store import JobsStore
from services.concurrency import output_retention_days

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
ARCHIVES_DIRNAME = "batch_archives"


async def sweep_expired_jobs(
    store: JobsStore,
    output_dir: Path,
    retention: timedelta,
    now: datetime,
) -> list[str]:
    """Remove every terminal job that finished before ``now - retention``.
    Returns the ids removed."""
    archives_root = (Path(output_dir) / ARCHIVES_DIRNAME).resolve()
    removed: list[str] = []
    for job_id in await store.finished_before(now - retention):
        archive = (archives_root / job_id).resolve()
        # A job id is a uuid hex; anything that would resolve outside
        # batch_archives (or to it) is left alone along with its record.
        if archive.parent != archives_root:
            logger.warning("Retention skipped job %r: its archive path is not under %s",
                           job_id, archives_root)
            continue
        await asyncio.to_thread(shutil.rmtree, archive, ignore_errors=True)
        if archive.exists():
            # Keep the record so the next sweep tries the folder again.
            logger.warning("Retention could not delete %s; will retry next sweep", archive)
            continue
        await store.remove(job_id)
        removed.append(job_id)
    logger.info(
        "Retention sweep removed %d audio job(s) finished more than %s ago",
        len(removed), retention,
    )
    return removed


async def sweep_once(store: JobsStore, output_dir: Path, now: datetime | None = None) -> None:
    """One sweep with the configured window, never raising: a failure is
    logged so the loop that calls this keeps going."""
    try:
        await sweep_expired_jobs(
            store,
            output_dir,
            timedelta(days=output_retention_days()),
            now or datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("Retention sweep failed; will try again next interval")


async def retention_loop(
    store: JobsStore,
    output_dir: Path,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep now, then every ``interval`` seconds, until cancelled."""
    while True:
        await sweep_once(store, output_dir)
        await sleep(interval)
