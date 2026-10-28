"""MediaCache: each file fetched once, never served half-written, kept to its cap."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from services.video_pipeline.media_cache import MediaCache


def _writer(body: bytes, calls: list):
    async def fetch(dest):
        calls.append(dest.name)
        await asyncio.sleep(0)
        dest.write_bytes(body)
    return fetch


def test_a_hit_does_not_fetch_again(tmp_path) -> None:
    cache = MediaCache(tmp_path, suffix=".img")
    calls: list = []

    async def _go():
        a = await cache.get("v1", _writer(b"x", calls))
        b = await cache.get("v1", _writer(b"y", calls))
        return a, b

    a, b = asyncio.run(_go())

    assert a == b and a.read_bytes() == b"x"
    assert calls == ["v1.img.part"]


def test_concurrent_misses_share_one_fetch(tmp_path) -> None:
    cache = MediaCache(tmp_path, suffix=".img")
    calls: list = []

    async def _go():
        return await asyncio.gather(*(cache.get("v1", _writer(b"x", calls)) for _ in range(20)))

    assert len(set(asyncio.run(_go()))) == 1
    assert len(calls) == 1


def test_a_failed_fetch_leaves_nothing_behind_and_can_be_retried(tmp_path) -> None:
    cache = MediaCache(tmp_path, suffix=".img")

    async def broken(dest):
        dest.write_bytes(b"half")
        raise RuntimeError("connection dropped")

    async def _go():
        with pytest.raises(RuntimeError):
            await cache.get("v1", broken)
        assert cache.cached("v1") is None
        assert list(tmp_path.iterdir()) == []
        return await cache.get("v1", _writer(b"whole", []))

    assert asyncio.run(_go()).read_bytes() == b"whole"


def test_an_empty_fetch_is_not_cached(tmp_path) -> None:
    cache = MediaCache(tmp_path, suffix=".img")

    async def empty(dest):
        dest.write_bytes(b"")

    with pytest.raises(RuntimeError, match="empty"):
        asyncio.run(cache.get("v1", empty))
    assert cache.cached("v1") is None


def test_past_its_cap_the_least_recently_used_go_first(tmp_path) -> None:
    cache = MediaCache(tmp_path, suffix=".mp4", max_bytes=25)

    async def _go():
        for key in ("a", "b"):
            await cache.get(key, _writer(b"0123456789", []))
        # 'a' is older on disk, but used since — so 'b' is the one to go.
        old = time.time() - 100
        os.utime(tmp_path / "a.mp4", (old, old))
        os.utime(tmp_path / "b.mp4", (old + 1, old + 1))
        cache.cached("a")
        await cache.get("c", _writer(b"0123456789", []))

    asyncio.run(_go())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.mp4", "c.mp4"]


@pytest.mark.parametrize("key", ["", "../etc", "a/b", ".hidden"])
def test_a_key_cannot_leave_the_cache_folder(tmp_path, key) -> None:
    with pytest.raises(ValueError):
        MediaCache(tmp_path, suffix=".img").cached(key)
