from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

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

    def __init__(self, persist_dir: Path | str | None = None) -> None:
        self._jobs: dict[str, VideoJobState] = {}
        self._lock = asyncio.Lock()
        self._persist_dir = Path(persist_dir) if persist_dir else None
        if self._persist_dir is not None:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    # --- persistence -----------------------------------------------------
    def _path(self, job_id: str) -> Path:
        assert self._persist_dir is not None
        return self._persist_dir / f"{job_id}.json"

    def _write(self, state: VideoJobState) -> None:
        """Atomically mirror one job's state to disk. Best-effort: a persistence
        failure is logged, never allowed to break the running job."""
        if self._persist_dir is None:
            return
        try:
            path = self._path(state.job_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(state.model_dump_json())
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("VideoJob %s: failed to persist state: %s", state.job_id, exc)

    def _load(self) -> None:
        assert self._persist_dir is not None
        count = 0
        for f in self._persist_dir.glob("*.json"):
            try:
                state = VideoJobState.model_validate_json(f.read_text())
                self._jobs[state.job_id] = state
                count += 1
            except Exception as exc:
                logger.warning("Skipping unreadable persisted job %s: %s", f.name, exc)
        if count:
            logger.info("Loaded %d persisted video jobs from %s", count, self._persist_dir)

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
