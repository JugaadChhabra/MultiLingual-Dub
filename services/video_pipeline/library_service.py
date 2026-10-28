"""What the library's routes ask for, with the caching that makes it cheap.

``library.py`` holds the rules (which state a render is in); this holds the
moving parts and keeps each expensive thing from being done twice:

- the account listing — ``RenderCatalog``, refreshed incrementally and in the
  background;
- the library itself — rebuilt only when the listing or the job store has
  changed (both carry a version), so the page polling every few seconds costs
  a dictionary lookup, not a copy of every job;
- thumbnails and raw renders — ``MediaCache`` on disk, each fetched from HeyGen
  once. The browser is told they never change, so it asks only once too.

The renderer is passed per call, so a missing HeyGen key fails that request
rather than the service at start-up.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from services.video_pipeline.library import LibraryEntry, RenderCatalog, build_library, entry_for
from services.video_pipeline.media_cache import MediaCache
from services.video_pipeline.renderer import VideoRenderer
from services.video_pipeline.store import VideoJobsStore

logger = logging.getLogger(__name__)

# Thumbnails are ~60 KB (2,700 renders is ~160 MB), so they are kept for good.
# Raw renders are ~30 MB each and only kept for repeat downloads, so they are capped.
VIDEO_CACHE_BYTES = 5 * 1024**3


class NoMedia(LookupError):
    """There is nothing to hand over for this render (yet, or ever)."""


@dataclass(frozen=True)
class Library:
    entries: list[LibraryEntry]
    listing_error: str | None


@dataclass(frozen=True)
class Download:
    path: Path
    filename: str


class LibraryService:
    def __init__(self, jobs_store: VideoJobsStore, *, jobs_dir: Path, cache_dir: Path,
                 catalog: RenderCatalog | None = None,
                 video_cache_bytes: int | None = VIDEO_CACHE_BYTES) -> None:
        """``jobs_dir`` is where video jobs keep their files (one folder per
        job id); ``cache_dir`` is the library's own."""
        self.jobs_store = jobs_store
        self.jobs_dir = Path(jobs_dir)
        self.catalog = catalog or RenderCatalog()
        self.thumbnails = MediaCache(cache_dir / "thumbs", suffix=".img", concurrency=4)
        self.videos = MediaCache(cache_dir / "videos", suffix=".mp4",
                                 max_bytes=video_cache_bytes, concurrency=2)
        self._built: tuple[tuple[int, int, bool], list[LibraryEntry]] | None = None

    # --- the list --------------------------------------------------------
    async def library(self, renderer: VideoRenderer, *, refresh: bool = False) -> Library:
        """Every entry, newest first. A listing failure still returns every job
        we know of, with the reason HeyGen's half is missing."""
        listing_error = None
        renders = []
        try:
            renders = await self.catalog.renders(renderer, refresh=refresh)
        except Exception as exc:
            logger.warning("HeyGen render listing failed: %s", exc)
            listing_error = f"Could not list renders on HeyGen: {exc}"

        key = (self.catalog.version, self.jobs_store.version, listing_error is None)
        if self._built is None or self._built[0] != key:
            self._built = (key, build_library(renders, await self.jobs_store.snapshot()))
        return Library(self._built[1], listing_error)

    async def entry(self, renderer: VideoRenderer, video_id: str) -> LibraryEntry | None:
        """One render's entry, from only its own render and jobs."""
        await self._listing(renderer)
        jobs = await self.jobs_store.jobs_for_video(video_id)
        return entry_for(video_id, self.catalog.get(video_id), jobs)

    async def _listing(self, renderer: VideoRenderer) -> None:
        """Make sure the catalog has a listing; carry on without one."""
        try:
            await self.catalog.renders(renderer)
        except Exception as exc:
            logger.warning("HeyGen render listing failed: %s", exc)

    # --- files -----------------------------------------------------------
    async def thumbnail(self, renderer: VideoRenderer, video_id: str) -> Path:
        """The render's thumbnail on disk, fetched from HeyGen the first time."""
        hit = self.thumbnails.cached(video_id)
        if hit is not None:
            return hit
        # Refuse an unfinished render from the listing, before spending a
        # status lookup on it. Without a listing, the lookup itself decides.
        await self._listing(renderer)
        listed = self.catalog.get(video_id)
        if listed is not None and listed.status != "completed":
            raise NoMedia("Only a finished render has a thumbnail")

        async def fetch(dest: Path) -> None:
            media = await renderer.render_media(video_id=video_id)
            if media.status != "completed" or not media.thumbnail_url:
                raise NoMedia("HeyGen has no thumbnail for this render")
            await renderer.download(video_url=media.thumbnail_url, dest_path=dest)

        return await self.thumbnails.get(video_id, fetch)

    async def download(self, renderer: VideoRenderer, video_id: str) -> Download:
        """The video as a file. What a job of ours filed (cards and music
        included) when we still have it; otherwise the raw render, fetched from
        HeyGen once and kept."""
        entry = await self.entry(renderer, video_id)
        if entry is None:
            raise LookupError("No such render on HeyGen or in our jobs")
        filename = f"{entry.title or video_id}.mp4"

        if entry.state == "on_nas" and entry.job_id:
            for name in ("video_carded.mp4", "video.mp4"):
                path = self.jobs_dir / entry.job_id / name
                if path.is_file() and path.stat().st_size > 0:
                    return Download(path, filename)

        hit = self.videos.cached(video_id)
        if hit is not None:
            return Download(hit, filename)
        render = self.catalog.get(video_id)
        if render is None or render.status != "completed":
            raise NoMedia(entry.reason or "HeyGen has no finished video for this yet")

        async def fetch(dest: Path) -> None:
            media = await renderer.render_media(video_id=video_id)
            if media.status != "completed" or not media.video_url:
                raise NoMedia("HeyGen has no finished video for this yet")
            await renderer.download(video_url=media.video_url, dest_path=dest)

        return Download(await self.videos.get(video_id, fetch), filename)


def sniff_image_type(path: Path) -> str:
    """HeyGen serves thumbnails as binary/octet-stream; say what they are."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"
