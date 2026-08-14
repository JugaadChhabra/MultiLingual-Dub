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


class VideoBatchJobState(BaseModel):
    batch_id: str
    status: Literal["queued", "running", "completed", "partial", "failed"] = "queued"
    total: int = 0
    done: int = 0
    failed_count: int = 0
    rows: list[BatchRowState] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


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
        self._jobs = self._mirror.load()
        for state in self._jobs.values():
            if state.status not in self.IN_FLIGHT_STATUSES:
                continue
            for row in state.rows:
                if row.status in ("pending", "running"):
                    row.status = "failed"
                    row.error = "Interrupted by a restart"
                    state.failed_count += 1
            state.status = "failed" if state.done == 0 else "partial"
            state.finished_at = datetime.now(timezone.utc)
            self._write(state)
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
            self._write(state)
            logger.info(
                "VideoBatch %s → %s (%d succeeded, %d failed)",
                batch_id, state.status, state.done, state.failed_count,
            )
