"""Recovery's rules: which video jobs may be recovered, and why one may not.

A render is paid for at submission, so a job that failed after its render
finished is worth re-running from the download onward. That is only safe for a
job that has actually failed, still has its render id and spec, whose render
HeyGen did not fail, and is not already being recovered — recovering a completed job would file it to the NAS a
second time, and two recoveries at once would race each other there.

These rules live here and nowhere else. The store applies them under its lock
when it lists recoverable jobs and when it claims one (``claim_recovery``); the
pipeline's ``start_recovery`` and ``recover_video_job`` claim through it. This
module imports only types, so the store can depend on it.
"""
from __future__ import annotations

from typing import Literal

from services.video_pipeline.types import VideoJobState

# The in-flight status a claimed job sits in until its recovery starts polling.
# Not 'failed', so a second claim is refused and recover-all passes it by.
RECOVERING = "recovering"


class NotRecoverable(ValueError):
    """Why a job can't be recovered. ``kind`` is 'not_found' when there is no
    such job, and 'conflict' when the job exists but is in the wrong state."""

    def __init__(self, reason: str, kind: Literal["not_found", "conflict"] = "conflict") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


def check_recoverable(state: VideoJobState | None) -> None:
    """Raise NotRecoverable unless this job may be recovered now."""
    if state is None:
        raise NotRecoverable("Job not found", "not_found")
    if state.status == RECOVERING:
        raise NotRecoverable("Job is already being recovered")
    if state.status != "failed":
        raise NotRecoverable(
            f"Job has not failed (status: {state.status}); only a failed job can be recovered"
        )
    if not state.summary.heygen_video_id:
        raise NotRecoverable("Job has no HeyGen render to recover")
    if state.summary.render_failed:
        raise NotRecoverable(
            "HeyGen's render failed, so there is no video to recover; re-run it instead"
        )
    if state.spec is None:
        raise NotRecoverable("Job has no persisted spec to recover from")


def is_recoverable(state: VideoJobState) -> bool:
    try:
        check_recoverable(state)
    except NotRecoverable:
        return False
    return True
