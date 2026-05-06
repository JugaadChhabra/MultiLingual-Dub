from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from services.video_pipeline.types import VideoJobState, VideoJobSummary

logger = logging.getLogger(__name__)


def _finalize(summary: VideoJobSummary) -> None:
    summary.finished_at = datetime.now(timezone.utc)
    if summary.started_at:
        delta = summary.finished_at - summary.started_at
        summary.duration_ms = int(delta.total_seconds() * 1000)


class VideoJobsStore:
    def __init__(self) -> None:
        self._jobs: dict[str, VideoJobState] = {}
        self._lock = asyncio.Lock()

    async def create(self, job_id: str) -> VideoJobState:
        async with self._lock:
            state = VideoJobState(job_id=job_id, status="queued", summary=VideoJobSummary())
            self._jobs[job_id] = state
            return state

    async def get(self, job_id: str) -> VideoJobState | None:
        async with self._lock:
            state = self._jobs.get(job_id)
            return state.model_copy(deep=True) if state else None

    async def set_status(self, job_id: str, status: str, message: str = "") -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = status
            state.stage_message = message
            if status == "tts" and state.summary.started_at is None:
                state.summary.started_at = datetime.now(timezone.utc)
            logger.info("VideoJob %s → %s | %s", job_id, status, message)

    async def patch_summary(self, job_id: str, **fields) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            for k, v in fields.items():
                setattr(state.summary, k, v)

    async def complete(self, job_id: str) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "completed"
            _finalize(state.summary)
            logger.info("VideoJob %s → completed", job_id)

    async def fail(self, job_id: str, message: str) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "failed"
            state.error = message
            _finalize(state.summary)
            logger.error("VideoJob %s → failed | %s", job_id, message)
