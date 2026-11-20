"""Admission: the one step both audio job routes (Excel and story) pass before
a job is created. It turns away requests that would overload the shared
machine, with an HTTPException naming the reason:

- 503 with ``Retry-After`` when ``MAX_QUEUED_JOBS`` jobs already wait in the
  job queue (checked first, before the sheet is copied or read);
- 507 when the output volume has less than ``MIN_FREE_DISK_GB`` free (also
  before the sheet is copied);
- for an Excel job, 413 when the upload is over ``MAX_UPLOAD_MB`` (copied in
  chunks, never held whole in memory) and 400 when the sheet has more than
  ``MAX_SHEET_ROWS`` rows (rows the job would voice, i.e. with text).

Starlette has already spooled the request body to its own temp file before
any route code runs, so these caps protect memory, the job queue and the work
done per request, not the bandwidth of the upload itself.

Nothing is created in the jobs store until admission passes, and every
rejection removes the temp file it made. On success the saved sheet belongs to
the job, which deletes it once read.

To add a check, add it to ``admit_audio_job``: checks that
need no upload go before the save, the rest after it, inside the ``try``.

The queue check is a soft cap: requests admitted at the same moment can each
see room for one more and slightly overshoot it.
"""
from __future__ import annotations

import asyncio
import logging
import math
import shutil
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from api import shared
from api.utils import save_upload_file
from batch.excel import read_excel_rows
from services.concurrency import (
    max_queued_jobs,
    max_sheet_rows,
    max_upload_mb,
    min_free_disk_gb,
)

logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = 60
GB = 1024 ** 3


async def admit_audio_job(excel: UploadFile | None = None) -> Path | None:
    """Admit one audio job, or raise HTTPException saying why not.

    With ``excel``, saves it to a temp .xlsx and returns that path (the caller
    hands it to the job). Without, returns None."""
    _check_queue_room()
    _check_free_disk()
    if excel is None:
        return None

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    saved_path = Path(temp_file.name)
    temp_file.close()
    try:
        await save_upload_file(excel, saved_path, max_mb=max_upload_mb())
        await _check_sheet_rows(saved_path)
    except BaseException:
        saved_path.unlink(missing_ok=True)
        raise
    return saved_path


def _check_queue_room() -> None:
    limit = max_queued_jobs()
    if shared.job_queue.waiting_count() >= limit:
        raise HTTPException(
            status_code=503,
            detail=(
                f"The server is busy: {limit} audio jobs are already waiting. "
                "Try again in a minute."
            ),
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )


def free_disk_bytes(path: Path) -> int:
    """Free bytes on the volume holding ``path``. Its own function so tests
    can fake a full or empty disk."""
    return shutil.disk_usage(path).free


def _check_free_disk() -> None:
    free_gb = free_disk_bytes(shared.OUTPUT_DIR) / GB
    if free_gb < min_free_disk_gb():
        # Rounded down, so 4.96 GB free never reads as "5.0 GB" against a 5 GB minimum.
        free_gb = math.floor(free_gb * 10) / 10
        raise HTTPException(
            status_code=507,
            detail=(
                f"The server is almost out of disk space ({free_gb:.1f} GB free); "
                "ask the maintainer to clear space before starting new runs."
            ),
        )


async def _check_sheet_rows(path: Path) -> None:
    """Count rows with the job's own reader, so "a row" means the same thing
    here as when the job runs."""
    try:
        rows = await asyncio.to_thread(read_excel_rows, path)
    except Exception:
        # A sheet the reader can't parse (bad headers, not a workbook) is left
        # to the job, which fails with the reader's own message, as before.
        logger.info("Admission could not count rows in %s; the job will report why", path)
        return
    limit = max_sheet_rows()
    if len(rows) > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This sheet has {len(rows):,} rows; the limit is {limit:,}. "
                "Split it into smaller sheets."
            ),
        )
