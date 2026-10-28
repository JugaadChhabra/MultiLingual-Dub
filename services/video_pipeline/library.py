"""The library: every render on the HeyGen account, matched to the job that
made it, each with what can be done with it now.

HeyGen keeps renders (the account still serves its first, from March 2026) but
not our titles — ``video_title`` comes back as the video id. So the title,
character and publish date come from our job records, joined on
``heygen_video_id``. A render no job of ours accounts for can still be
downloaded, and filed to the NAS once someone supplies those details.

Each entry is in exactly one state:

- ``on_nas``           filed; nothing to do
- ``in_progress``      a job (or a recovery of one) is running now
- ``sendable``         finished on HeyGen, never reached the NAS; Recovery can file it
- ``needs_details``    finished on HeyGen, but we don't know what to call it
- ``rendering``        HeyGen is still working on it
- ``not_recoverable``  there is no video to file; ``reason`` says why

``build_library`` decides that and nothing else. ``RenderCatalog`` keeps the
account's listing between page loads, since the full listing is ~30 requests.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from services.video_pipeline.recovery import RECOVERING, NotRecoverable, check_recoverable
from services.video_pipeline.renderer import RenderListing, VideoRenderer
from services.video_pipeline.types import VideoJobState

LibraryState = Literal["on_nas", "in_progress", "sendable", "needs_details", "rendering", "not_recoverable"]

RENDER_FAILED = "HeyGen's render failed, so there is no video to recover. Re-run it to make a new one."
NOTHING_RENDERED = "Failed before anything was sent to HeyGen, so nothing was rendered. Re-run it."
NOT_ON_HEYGEN = "HeyGen no longer has this render, so it can't be recovered. Re-run it."


@dataclass(frozen=True)
class LibraryEntry:
    video_id: str | None
    job_id: str | None
    state: LibraryState
    created_at: datetime | None
    title: str | None = None
    character: str | None = None
    publish_date: str | None = None
    nas_path: str | None = None
    # Why it is not recoverable, or what went wrong last time it was tried.
    reason: str | None = None
    # What a job in progress is doing now.
    stage: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        return d


_TERMINAL = ("completed", "failed")
# When several jobs claim one render (a render filed again under new details),
# the one that says the most about it wins.
_RANK = {"completed": 0, RECOVERING: 1, "failed": 3}


def _rank(state: VideoJobState) -> int:
    return _RANK.get(state.status, 2 if state.status not in _TERMINAL else 3)


def _from_job(state: VideoJobState, render: RenderListing | None) -> LibraryEntry:
    spec = state.spec
    base = dict(
        video_id=state.summary.heygen_video_id,
        job_id=state.job_id,
        created_at=(render.created_at if render else None) or state.summary.started_at,
        title=spec.video_title if spec else None,
        character=spec.character if spec else None,
        publish_date=spec.publish_date if spec else None,
        nas_path=state.summary.nas_path,
    )
    if state.status == "completed":
        return LibraryEntry(state="on_nas", **base)
    if state.status != "failed":
        return LibraryEntry(state="in_progress", stage=state.stage_message or state.status, **base)

    if not state.summary.heygen_video_id:
        detail = f" ({state.error})" if state.error else ""
        return LibraryEntry(state="not_recoverable", reason=NOTHING_RENDERED + detail, **base)
    if state.summary.render_failed or (render and render.status == "failed"):
        return LibraryEntry(state="not_recoverable", reason=RENDER_FAILED, **base)
    if render is None:
        return LibraryEntry(state="not_recoverable", reason=NOT_ON_HEYGEN, **base)
    if render.status == "processing":
        return LibraryEntry(state="rendering", **base)
    if state.spec is None:
        return LibraryEntry(state="needs_details", **base)
    try:
        check_recoverable(state)
    except NotRecoverable as exc:
        return LibraryEntry(state="not_recoverable", reason=exc.reason, **base)
    return LibraryEntry(state="sendable", reason=state.error, **base)


def _from_render(render: RenderListing) -> LibraryEntry:
    base = dict(video_id=render.video_id, job_id=None, created_at=render.created_at)
    if render.status == "failed":
        return LibraryEntry(state="not_recoverable", reason=RENDER_FAILED, **base)
    if render.status == "processing":
        return LibraryEntry(state="rendering", **base)
    return LibraryEntry(state="needs_details", **base)


def build_library(renders: list[RenderListing], jobs: list[VideoJobState]) -> list[LibraryEntry]:
    """One entry per render on the account, plus one per failed job HeyGen has
    no render for (so it can say why it can't be recovered). Newest first."""
    by_video: dict[str, VideoJobState] = {}
    unmatched: list[VideoJobState] = []
    for job in jobs:
        vid = job.summary.heygen_video_id
        if not vid:
            unmatched.append(job)
        elif vid not in by_video or _rank(job) < _rank(by_video[vid]):
            by_video[vid] = job

    entries: list[LibraryEntry] = []
    seen: set[str] = set()
    for render in renders:
        seen.add(render.video_id)
        job = by_video.get(render.video_id)
        entries.append(_from_job(job, render) if job else _from_render(render))
    for vid, job in by_video.items():
        if vid not in seen:
            entries.append(_from_job(job, None))
    for job in unmatched:
        # A job still before submission is in progress; one that failed there
        # is worth a line saying it can't be recovered. Completed jobs always
        # have a render id.
        if job.status != "completed":
            entries.append(_from_job(job, None))

    epoch = datetime.min
    entries.sort(key=lambda e: (e.created_at.replace(tzinfo=None) if e.created_at else epoch), reverse=True)
    return entries


class RenderCatalog:
    """The account's render listing, kept between page loads.

    Refreshing re-lists only until it reaches renders already known to be
    finished, so after the first load a refresh is usually one request.
    """

    def __init__(self, max_age_seconds: float = 60.0) -> None:
        self._renders: dict[str, RenderListing] = {}
        self._fetched_at: float | None = None
        self._max_age = max_age_seconds
        self._lock = asyncio.Lock()

    async def renders(self, renderer: VideoRenderer, *, refresh: bool = False) -> list[RenderListing]:
        async with self._lock:
            stale = self._fetched_at is None or time.monotonic() - self._fetched_at > self._max_age
            if refresh or stale:
                known = frozenset(vid for vid, r in self._renders.items() if r.status != "processing")
                fresh = await renderer.list_renders(known=known)
                merged = {r.video_id: r for r in fresh}
                # Older renders the early stop skipped are still on the account.
                for vid, r in self._renders.items():
                    merged.setdefault(vid, r)
                self._renders = merged
                self._fetched_at = time.monotonic()
            return list(self._renders.values())

    def get(self, video_id: str) -> RenderListing | None:
        return self._renders.get(video_id)
