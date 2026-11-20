"""Retention (video): job folders are removed once they are old enough.

A sweep removes, under the video output folder (``output/heygen/``):

- the folder ``<job_id>/`` of every video job that ended — completed, failed or
  cancelled — more than the window ago (``VIDEO_OUTPUT_RETENTION_HOURS``,
  default 48; only job-id-shaped names, 32 lowercase hex);
- a folder named like a job id (32 lowercase hex) that has no job record, once
  its modification time is older than the window.

It never removes a job record: the Library and Recovery depend on them. It
never touches an in-flight job's folder (including ``recovering``, which still
carries the finish time of its earlier failure), a terminal job with no finish
time, any ``_``-prefixed folder (job store, batch store, Library cache, script
history, TTS cache), a file at the top level, a symlink, or anything whose
resolved path is not a direct child of the video output folder.

A folder that could not be deleted is logged and tried again next sweep. The
audio pipeline's retention (``batch/retention.py``) is separate.

The app runs ``video_retention_loop`` from start-up: one sweep at once, then
one every six hours. A sweep that fails is logged and the next one still runs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from services.concurrency import video_output_retention_hours
from services.video_pipeline.store import VideoJobsStore

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 6 * 60 * 60

_JOB_ID_NAME = re.compile(r"[0-9a-f]{32}")


# Far beyond any sensible window, and small enough that ``now - window`` can't
# overflow a datetime.
_MAX_RETENTION_HOURS = 100 * 365 * 24


def _sweepable(root: Path, name: str) -> Path | None:
    """The folder ``root/name`` if the sweep may delete it, else None.

    ``root`` must already be resolved. Only job-id-shaped names (which rules
    out the ``_`` app-state folders); never a symlink, a file, or anything that
    does not resolve to a direct child of root.
    """
    if not _JOB_ID_NAME.fullmatch(name):
        return None
    folder = root / name
    if folder.is_symlink() or not folder.is_dir():
        return None
    if folder.resolve().parent != root:
        logger.warning("Video retention skipped %r: it is not a folder directly under %s",
                       name, root)
        return None
    return folder


def _candidates(root: Path, expired: set[str], known: set[str], cutoff_ts: float) -> list[Path]:
    """One listing of the output folder: the expired jobs' folders and the
    orphans past the window. Blocking; run in a thread."""
    found: list[Path] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name in known and name not in expired:
            continue
        folder = _sweepable(root, name)
        if folder is None:
            continue
        if name not in known:
            try:
                if folder.stat().st_mtime >= cutoff_ts:
                    continue
            except FileNotFoundError:
                continue
        found.append(folder)
    return found


def _delete(folder: Path) -> bool:
    """Delete ``folder``; True if it is gone afterwards. Blocking."""
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        logger.warning("Video retention could not delete %s; will retry next sweep", folder)
        return False
    return True


async def sweep_expired_job_folders(
    store: VideoJobsStore,
    video_output_dir: Path,
    retention: timedelta,
    now: datetime,
) -> list[str]:
    """Remove expired job folders (see the module docstring). Returns the
    names of the folders removed; job records are left as they are."""
    root = Path(video_output_dir).resolve()
    cutoff = now - retention
    removed: list[str] = []
    orphans = 0
    if root.is_dir():
        # Orphans are judged against every record taken *before* the listing,
        # so a job created meanwhile only risks a folder whose mtime is fresh.
        known = await store.job_ids()
        expired = set(await store.finished_before(cutoff))
        candidates = await asyncio.to_thread(
            _candidates, root, expired, known, cutoff.timestamp())
        for folder in candidates:
            name = folder.name
            # Re-check right before deleting: Recovery may have claimed the
            # job (or a record appeared for an orphan) since the listing.
            if name in expired:
                if not await store.finished_before(cutoff, job_id=name):
                    continue
            elif name in await store.job_ids():
                continue
            if await asyncio.to_thread(_delete, folder):
                removed.append(name)
                orphans += name not in expired
    logger.info(
        "Video retention sweep removed %d job folder(s) (%d orphan) older than %s",
        len(removed), orphans, retention,
    )
    return removed


async def sweep_once(store: VideoJobsStore, video_output_dir: Path) -> None:
    """One sweep with the configured window, never raising: a failure is
    logged so the loop that calls this keeps going."""
    try:
        await sweep_expired_job_folders(
            store,
            video_output_dir,
            timedelta(hours=min(video_output_retention_hours(), _MAX_RETENTION_HOURS)),
            datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("Video retention sweep failed; will try again next interval")


async def video_retention_loop(
    store: VideoJobsStore,
    video_output_dir: Path,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep now, then every ``interval`` seconds, until cancelled."""
    while True:
        await sweep_once(store, video_output_dir)
        await sleep(interval)
