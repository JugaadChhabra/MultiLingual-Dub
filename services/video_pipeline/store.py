from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from services.state_mirror import JsonStateMirror
from services.video_pipeline.types import VideoJobSpec, VideoJobState, VideoJobSummary

logger = logging.getLogger(__name__)


def _finalize(summary: VideoJobSummary) -> None:
    summary.finished_at = datetime.now(timezone.utc)
    if summary.started_at:
        delta = summary.finished_at - summary.started_at
        summary.duration_ms = int(delta.total_seconds() * 1000)


class VideoJobsStore:
    """In-memory job store with optional write-through JSON persistence.

    When ``persist_dir`` is set, every job's full state — including
    ``heygen_video_id``, ``video_url`` and the originating ``spec`` — is mirrored
    to ``{job_id}.json`` on each mutation and reloaded on startup. This is what
    lets a job that failed AFTER its HeyGen render finished (a transient
    download/NAS error that exhausted retries, or a process restart) be re-run
    later instead of silently losing the finished render.
    """

    # Statuses a job can still be sitting in when the process goes away. None of
    # them can be true of a reloaded job: the asyncio task that owned it died
    # with the process.
    IN_FLIGHT_STATUSES = ("queued", "tts", "uploading", "generating", "polling", "downloading", "nas_upload")

    def __init__(self, persist_dir: Path | str | None = None) -> None:
        self._jobs: dict[str, VideoJobState] = {}
        self._lock = asyncio.Lock()
        self._mirror: JsonStateMirror[VideoJobState] | None = None
        if persist_dir is not None:
            self._mirror = JsonStateMirror(persist_dir, VideoJobState, lambda s: s.job_id)
            self._load()

    # --- persistence -----------------------------------------------------
    def _write(self, state: VideoJobState) -> None:
        if self._mirror is not None:
            self._mirror.write(state)

    def _load(self) -> None:
        """Reload persisted jobs, settling any that the restart interrupted.

        A job persisted as 'polling' is not polling any more — nothing is. Left
        as-is it would claim to be in progress forever AND stay invisible to
        list_recoverable, which only considers failed jobs. So its finished
        HeyGen render, already paid for, would be stranded. Marking it failed is
        what makes it recoverable.
        """
        assert self._mirror is not None
        self._jobs = self._mirror.load()
        for state in self._jobs.values():
            if state.status in self.IN_FLIGHT_STATUSES:
                state.status = "failed"
                state.error = f"Interrupted by a restart while {state.stage_message or state.status}"
                _finalize(state.summary)
                self._write(state)
                logger.warning(
                    "VideoJob %s was in flight at shutdown → marked failed (recoverable=%s)",
                    state.job_id, bool(state.summary.heygen_video_id),
                )

    # --- mutations -------------------------------------------------------
    async def create(self, job_id: str) -> VideoJobState:
        async with self._lock:
            state = VideoJobState(job_id=job_id, status="queued", summary=VideoJobSummary())
            self._jobs[job_id] = state
            self._write(state)
            return state

    async def set_spec(self, job_id: str, spec: VideoJobSpec) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.spec = spec
            self._write(state)

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
            self._write(state)
            logger.info("VideoJob %s → %s | %s", job_id, status, message)

    async def patch_summary(self, job_id: str, **fields) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            for k, v in fields.items():
                setattr(state.summary, k, v)
            self._write(state)

    async def complete(self, job_id: str) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "completed"
            state.error = None
            _finalize(state.summary)
            self._write(state)
            logger.info("VideoJob %s → completed", job_id)

    async def fail(self, job_id: str, message: str) -> None:
        async with self._lock:
            state = self._jobs[job_id]
            state.status = "failed"
            state.error = message
            _finalize(state.summary)
            self._write(state)
            logger.error("VideoJob %s → failed | %s", job_id, message)

    # --- recovery --------------------------------------------------------
    async def list_recoverable(self) -> list[str]:
        """job_ids of failed jobs whose HeyGen render actually finished — i.e. a
        heygen_video_id is on file — so the download/NAS tail can be re-run."""
        async with self._lock:
            return [
                jid for jid, s in self._jobs.items()
                if s.status == "failed" and s.summary.heygen_video_id
            ]
