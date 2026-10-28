from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from services.state_mirror import JsonStateMirror

logger = logging.getLogger(__name__)


class BatchRowState(BaseModel):
    row_index: int
    script: str
    video_title: str
    job_id: str = ""
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    video_local_url: str | None = None
    nas_path: str | None = None
    error: str | None = None
    # The row's video job's classified cause, when it got far enough to have one
    cause: dict | None = None


class VideoBatchJobState(BaseModel):
    batch_id: str
    status: Literal["queued", "running", "completed", "partial", "failed"] = "queued"
    total: int = 0
    done: int = 0
    failed_count: int = 0
    rows: list[BatchRowState] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cause: dict | None = None   # why a failed/partial batch failed, see batch_cause


def batch_cause(state: VideoBatchJobState) -> dict | None:
    """What the operator is told about a failed or partial batch.

    Built only from what the rows recorded, so it claims nothing they did not:
    one shared error reuses that row's classified cause; anything else is a
    count plus the rows' own words. No row error means no cause at all.
    """
    failed = [r for r in state.rows if r.status == "failed" and r.error]
    if not failed:
        return None
    errors = {r.error for r in failed}
    if len(errors) == 1:
        classified = next((r.cause for r in failed if r.cause), None)
        if classified:
            return classified
    return {
        "kind": "batch_rows_failed",
        "severity": "error",
        "title": f"{len(failed)} of {state.total} rows failed",
        "detail": failed[0].error,
        "raw": "\n".join(f"row {r.row_index}: {r.error}" for r in failed),
        "retryable": False,
        "provider": None,
        "stage": None,
        "action_url": None,
        "action_label": None,
        "meta": {},
    }


class VideoBatchJobsStore:
    """Video batch jobs, optionally mirrored to disk.

    Per-row state is what makes the mirror worth having here: each row records
    the video job id that rendered it, so an interrupted batch still says which
    rows finished and where their videos landed — and the individual video jobs
    remain independently recoverable through VideoJobsStore.
    """

    IN_FLIGHT_STATUSES = ("queued", "running")

    def __init__(self, persist_dir: Path | str | None = None) -> None:
        self._jobs: dict[str, VideoBatchJobState] = {}
        self._lock = asyncio.Lock()
        self._mirror: JsonStateMirror[VideoBatchJobState] | None = None
        if persist_dir is not None:
            self._mirror = JsonStateMirror(persist_dir, VideoBatchJobState, lambda s: s.batch_id)
            self._load()

    def _write(self, state: VideoBatchJobState) -> None:
        if self._mirror is not None:
            self._mirror.write(state)

    def _load(self) -> None:
        """Reload persisted batches, settling any the restart interrupted.

        Rows still marked 'running' are settled too — their asyncio task is gone,
        so claiming otherwise would misreport the batch forever.
        """
        assert self._mirror is not None
        self._jobs = self._mirror.load_settled(self.IN_FLIGHT_STATUSES, self._settle)

    @staticmethod
    def _settle(state: VideoBatchJobState) -> None:
        for row in state.rows:
            if row.status in ("pending", "running"):
                row.status = "failed"
                row.error = "Interrupted by a restart"
                state.failed_count += 1
        state.status = "failed" if state.done == 0 else "partial"
        state.cause = batch_cause(state)
        state.finished_at = datetime.now(timezone.utc)
        logger.warning(
            "VideoBatch %s was in flight at shutdown → marked %s (%d done, %d failed)",
            state.batch_id, state.status, state.done, state.failed_count,
        )

    async def create(self, batch_id: str, rows: list[BatchRowState]) -> VideoBatchJobState:
        async with self._lock:
            state = VideoBatchJobState(batch_id=batch_id, total=len(rows), rows=rows)
            self._jobs[batch_id] = state
            self._write(state)
            return state.model_copy(deep=True)

    async def get(self, batch_id: str) -> VideoBatchJobState | None:
        async with self._lock:
            state = self._jobs.get(batch_id)
            return state.model_copy(deep=True) if state else None

    async def start(self, batch_id: str) -> None:
        async with self._lock:
            state = self._jobs[batch_id]
            state.status = "running"
            state.started_at = datetime.now(timezone.utc)
            self._write(state)

    async def update_row(self, batch_id: str, row_index: int, **fields) -> None:
        async with self._lock:
            state = self._jobs[batch_id]
            for row in state.rows:
                if row.row_index == row_index:
                    for k, v in fields.items():
                        setattr(row, k, v)
                    break
            self._write(state)

    async def row_succeeded(self, batch_id: str) -> None:
        async with self._lock:
            self._jobs[batch_id].done += 1
            self._write(self._jobs[batch_id])

    async def row_failed(self, batch_id: str) -> None:
        async with self._lock:
            self._jobs[batch_id].failed_count += 1
            self._write(self._jobs[batch_id])

    async def complete(self, batch_id: str) -> None:
        async with self._lock:
            state = self._jobs[batch_id]
            state.finished_at = datetime.now(timezone.utc)
            if state.failed_count == 0:
                state.status = "completed"
            elif state.done == 0:
                state.status = "failed"
            else:
                state.status = "partial"
            state.cause = batch_cause(state)
            self._write(state)
            logger.info(
                "VideoBatch %s → %s (%d succeeded, %d failed)",
                batch_id, state.status, state.done, state.failed_count,
            )

    async def row_recovered(self, job_id: str, *, nas_path: str | None,
                            video_local_url: str | None) -> bool:
        """Mark the failed row whose video job ``job_id`` was since recovered
        as done, and re-settle a finished batch's status and cause to match.
        Returns whether any row was rendered by that job."""
        async with self._lock:
            for state in self._jobs.values():
                row = next((r for r in state.rows
                            if r.job_id == job_id and r.status == "failed"), None)
                if row is None:
                    continue
                row.status = "completed"
                row.nas_path = nas_path
                row.video_local_url = video_local_url
                row.error = None
                row.cause = None
                state.done += 1
                state.failed_count = max(0, state.failed_count - 1)
                if state.finished_at is not None:
                    state.status = ("completed" if state.failed_count == 0
                                    else "failed" if state.done == 0 else "partial")
                    state.cause = batch_cause(state)
                self._write(state)
                logger.info("VideoBatch %s | row %d recovered (job %s) → %s",
                            state.batch_id, row.row_index, job_id, state.status)
                return True
            return False
