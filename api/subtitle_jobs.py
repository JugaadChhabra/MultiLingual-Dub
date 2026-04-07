from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from api.models import SubtitleJobState


class SubtitleJobsStore:
    def __init__(self) -> None:
        self._jobs: dict[str, SubtitleJobState] = {}
        self._lock = asyncio.Lock()

    async def create(self, job_id: str) -> SubtitleJobState:
        async with self._lock:
            state = SubtitleJobState(
                job_id=job_id,
                status="queued",
                progress_step="queued",
                progress_message="Job accepted and waiting to start",
                progress_percent=0,
            )
            self._jobs[job_id] = state
            return state

    async def get(self, job_id: str) -> SubtitleJobState | None:
        async with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return None
            return state.model_copy(deep=True)

    async def start(self, job_id: str) -> datetime:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "running"
            state.progress_step = "starting"
            state.progress_message = "Initializing subtitle pipeline"
            state.progress_percent = 1
            state.started_at = datetime.now(timezone.utc)
            return state.started_at

    async def update_progress(
        self,
        job_id: str,
        *,
        step: str,
        message: str,
        percent: int,
    ) -> None:
        bounded = max(0, min(percent, 99))
        async with self._lock:
            state = self._jobs[job_id]
            state.progress_step = step
            state.progress_message = message
            state.progress_percent = bounded

    async def complete(self, job_id: str, result: dict) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "completed"
            state.result = result
            state.progress_step = "completed"
            state.progress_message = "Subtitle pipeline completed"
            state.progress_percent = 100
            state.finished_at = datetime.now(timezone.utc)
            if state.started_at:
                delta = state.finished_at - state.started_at
                state.duration_ms = int(delta.total_seconds() * 1000)

    async def fail(self, job_id: str, error: str) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "failed"
            state.error = error
            state.progress_step = state.progress_step or "failed"
            state.progress_message = error
            state.finished_at = datetime.now(timezone.utc)
            if state.started_at:
                delta = state.finished_at - state.started_at
                state.duration_ms = int(delta.total_seconds() * 1000)
