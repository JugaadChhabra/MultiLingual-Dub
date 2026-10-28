"""Files fetched from the render provider, kept on disk so each is fetched once.

A render's thumbnail and video never change once it has finished, so a copy is
good forever: the cache is keyed by video id and never revalidates. What it
guarantees:

- **At most one fetch per key.** Concurrent requests for a key that is being
  fetched wait for that fetch instead of starting their own (single-flight). A
  page of fifty thumbnails asked for twice still costs fifty fetches, not 100.
- **A bounded number of fetches at once**, so a page of thumbnails doesn't
  open fifty connections to the provider.
- **No partial file is ever served.** A fetch writes ``<key>.part`` and renames
  it into place only once it has finished.
- **An optional size cap.** Past it, the least recently used files are evicted;
  a hit counts as a use.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Writes the file at the path it is given; raises if it cannot.
Fetch = Callable[[Path], Awaitable[None]]


class MediaCache:
    def __init__(self, root: Path, *, suffix: str, max_bytes: int | None = None,
                 concurrency: int = 4) -> None:
        self._root = Path(root)
        self._suffix = suffix
        self._max_bytes = max_bytes
        self._slots = asyncio.Semaphore(concurrency)
        self._inflight: dict[str, asyncio.Future[Path]] = {}

    def _path(self, key: str) -> Path:
        # Keys are provider ids; refuse anything that could leave the folder.
        if not key or "/" in key or "\\" in key or key.startswith("."):
            raise ValueError(f"invalid cache key: {key!r}")
        return self._root / f"{key}{self._suffix}"

    def cached(self, key: str) -> Path | None:
        """The cached file for ``key``, or None. Counts as a use for eviction."""
        path = self._path(key)
        if not path.is_file():
            return None
        if self._max_bytes is not None:
            try:
                os.utime(path)
            except OSError:
                pass
        return path

    async def get(self, key: str, fetch: Fetch) -> Path:
        """The file for ``key``, calling ``fetch`` only if no copy exists and no
        other caller is already fetching it."""
        hit = self.cached(key)
        if hit is not None:
            return hit
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)

        future: asyncio.Future[Path] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            path = await self._fetch(key, fetch)
        except BaseException as exc:
            future.set_exception(exc)
            # Nobody else may be waiting; don't let the event loop complain
            # about an exception that was never retrieved.
            future.exception()
            raise
        else:
            future.set_result(path)
            return path
        finally:
            del self._inflight[key]

    async def _fetch(self, key: str, fetch: Fetch) -> Path:
        path = self._path(key)
        part = path.with_name(path.name + ".part")
        async with self._slots:
            self._root.mkdir(parents=True, exist_ok=True)
            part.unlink(missing_ok=True)
            try:
                await fetch(part)
                if not part.is_file() or part.stat().st_size == 0:
                    raise RuntimeError("the provider returned an empty file")
                part.replace(path)
            finally:
                part.unlink(missing_ok=True)
        if self._max_bytes is not None:
            await asyncio.to_thread(self._evict, keep=path)
        return path

    def _evict(self, *, keep: Path) -> None:
        """Delete least-recently-used files until the cache fits its cap."""
        files = []
        for p in self._root.glob(f"*{self._suffix}"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        total = sum(size for _, size, _ in files)
        for _, size, p in sorted(files):
            if total <= self._max_bytes:
                break
            if p == keep:
                continue
            try:
                p.unlink()
                total -= size
                logger.info("Media cache: evicted %s (%d bytes)", p.name, size)
            except OSError:
                pass
