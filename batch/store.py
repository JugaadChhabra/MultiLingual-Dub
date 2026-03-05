from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from batch.models import JobState, JobSummary

logger = logging.getLogger(__name__)


class JobsStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = asyncio.Lock()

    async def create(self, job_id: str) -> JobState:
        async with self._lock:
            state = JobState(job_id=job_id, status="queued", summary=JobSummary())
            self._jobs[job_id] = state
            return state

    async def get(self, job_id: str) -> JobState | None:
        async with self._lock:
            state = self._jobs.get(job_id)
            if not state:
                return None
            return state.model_copy(deep=True)

    async def start(self, job_id: str) -> datetime:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "running"
            started_at = datetime.now(timezone.utc)
            state.summary.started_at = started_at
            logger.info("Job %s → running", job_id)
            return started_at

    async def update_summary(self, job_id: str, summary: JobSummary) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.summary = summary

    async def complete(self, job_id: str, summary: JobSummary) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "completed"
            summary.finished_at = datetime.now(timezone.utc)
            if summary.started_at:
                delta = summary.finished_at - summary.started_at
                summary.duration_ms = int(delta.total_seconds() * 1000)
            state.summary = summary
            logger.info(
                "Job %s → completed | rows=%d/%d tasks=%d/%d duration_ms=%s",
                job_id,
                summary.rows_succeeded,
                summary.total_rows,
                summary.language_tasks_succeeded,
                summary.language_tasks_total,
                summary.duration_ms,
            )

    async def fail(self, job_id: str, message: str, summary: JobSummary | None = None) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "failed"
            state.error = message
            if summary is None:
                summary = state.summary
            summary.finished_at = datetime.now(timezone.utc)
            if summary.started_at:
                delta = summary.finished_at - summary.started_at
                summary.duration_ms = int(delta.total_seconds() * 1000)
            state.summary = summary
            logger.error("Job %s → failed | %s", job_id, message)
