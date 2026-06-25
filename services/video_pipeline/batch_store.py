from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Literal

from pydantic import BaseModel, Field

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
    def __init__(self) -> None:
        self._jobs: dict[str, VideoBatchJobState] = {}
        self._lock = asyncio.Lock()

    async def create(self, batch_id: str, rows: list[BatchRowState]) -> VideoBatchJobState:
        async with self._lock:
            state = VideoBatchJobState(batch_id=batch_id, total=len(rows), rows=rows)
            self._jobs[batch_id] = state
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

    async def update_row(self, batch_id: str, row_index: int, **fields) -> None:
        async with self._lock:
            for row in self._jobs[batch_id].rows:
                if row.row_index == row_index:
                    for k, v in fields.items():
                        setattr(row, k, v)
                    break

    async def row_succeeded(self, batch_id: str) -> None:
        async with self._lock:
            self._jobs[batch_id].done += 1

    async def row_failed(self, batch_id: str) -> None:
        async with self._lock:
            self._jobs[batch_id].failed_count += 1

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
            logger.info(
                "VideoBatch %s → %s (%d succeeded, %d failed)",
                batch_id, state.status, state.done, state.failed_count,
            )
