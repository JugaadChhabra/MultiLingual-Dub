from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path

from services.errors import classify
from batch.models import JobState, JobSummary
from services.state_mirror import JsonStateMirror

logger = logging.getLogger(__name__)


def _finalize_summary(summary: JobSummary) -> None:
    summary.finished_at = datetime.now(timezone.utc)
    if summary.started_at:
        delta = summary.finished_at - summary.started_at
        summary.duration_ms = int(delta.total_seconds() * 1000)


class JobsStore:
    """Audio batch jobs, optionally mirrored to disk.

    The mirror is a RECORD, not a resume: an audio batch's work — translations,
    speech, and audio not yet uploaded — lives in memory and dies with the
    process. What survives is the account of how far it got, plus any local
    archive links for activities that finished, whose zips are already on disk.
    Without it an interrupted batch simply 404s and the user learns nothing.
    """

    IN_FLIGHT_STATUSES = ("queued", "running")

    def __init__(self, persist_dir: Path | str | None = None) -> None:
        self._jobs: dict[str, JobState] = {}
        self._cancel_flags: set[str] = set()
        self._lock = asyncio.Lock()
        self._mirror: JsonStateMirror[JobState] | None = None
        if persist_dir is not None:
            self._mirror = JsonStateMirror(persist_dir, JobState, lambda s: s.job_id)
            self._load()

    def _write(self, state: JobState) -> None:
        if self._mirror is not None:
            self._mirror.write(state)

    def _load(self) -> None:
        """Reload persisted jobs, settling any the restart interrupted."""
        assert self._mirror is not None
        self._jobs = self._mirror.load()
        for state in self._jobs.values():
            if state.status in self.IN_FLIGHT_STATUSES:
                state.status = "failed"
                state.error = "Interrupted by a restart"
                _finalize_summary(state.summary)
                self._write(state)
                logger.warning(
                    "Job %s was in flight at shutdown → marked failed (%d/%d rows done)",
                    state.job_id, state.summary.rows_processed, state.summary.total_rows,
                )

    async def create(self, job_id: str) -> JobState:
        async with self._lock:
            state = JobState(job_id=job_id, status="queued", summary=JobSummary())
            self._jobs[job_id] = state
            self._write(state)
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
            self._write(state)
            logger.info("Job %s → running", job_id)
            return started_at

    async def update_summary(self, job_id: str, summary: JobSummary) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.summary = summary
            self._write(state)

    async def complete(self, job_id: str, summary: JobSummary) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "completed"
            _finalize_summary(summary)
            state.summary = summary
            self._write(state)
            logger.info(
                "Job %s → completed | rows=%d/%d tasks=%d/%d duration_ms=%s",
                job_id,
                summary.rows_succeeded,
                summary.total_rows,
                summary.language_tasks_succeeded,
                summary.language_tasks_total,
                summary.duration_ms,
            )

    async def fail(
        self,
        job_id: str,
        message: str,
        summary: JobSummary | None = None,
        *,
        exc: BaseException | None = None,
        stage: str | None = None,
    ) -> None:
        """`exc` is what actually failed. Passing it lets the operator be told a
        cause instead of a string — see services/errors.py."""
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "failed"
            state.error = message
            if exc is not None:
                state.cause = classify(exc, provider=None, stage=stage).to_dict()
            if summary is None:
                summary = state.summary
            _finalize_summary(summary)
            state.summary = summary
            self._write(state)
            logger.error("Job %s → failed | %s", job_id, message)

    async def request_cancel(self, job_id: str) -> bool:
        async with self._lock:
            state = self._jobs.get(job_id)
            if not state or state.status not in ("queued", "running"):
                return False
            self._cancel_flags.add(job_id)
            logger.info("Job %s → cancel requested", job_id)
            return True

    async def is_cancelled(self, job_id: str) -> bool:
        async with self._lock:
            return job_id in self._cancel_flags

    async def cancel(self, job_id: str, summary: JobSummary | None = None) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "cancelled"
            state.error = "Job cancelled by user"
            self._cancel_flags.discard(job_id)
            if summary is None:
                summary = state.summary
            _finalize_summary(summary)
            state.summary = summary
            self._write(state)
            logger.info("Job %s → cancelled", job_id)
