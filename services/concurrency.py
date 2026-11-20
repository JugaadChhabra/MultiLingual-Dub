"""How wide this process runs: the ElevenLabs provider limit, the size of
the default thread pool, how many audio jobs run at once (the job queue's
slots, see batch/job_queue.py), and the per-folder S3 upload lock.

The provider limit is a process-wide cap on concurrent ElevenLabs calls.

Every audio job (Excel and story) speaks through ``speak_row``, which holds a
slot here around each language's TTS call, retries included. So however many
jobs run at once, ElevenLabs never sees more than ``ELEVENLABS_MAX_CONCURRENCY``
calls from this process (default 10, the Pro plan's limit). A call backing off
after a 429 keeps its slot; a 429 means we should be sending less anyway.

Both are read straight from the environment, like ``API_RETRY_*``: they are
process tuning, describing the deployment rather than a per-session setting.

The semaphore is created lazily, one per running event loop. An asyncio
semaphore is bound to the loop it is first used on, so a module-level one would
break the moment a second loop used it (each ``asyncio.run`` in the tests, for
one). In the app there is only ever one loop, so this is one shared semaphore.

The video pipeline's speech (services/video_pipeline/speech.py) does not go
through this limit yet; the video pipeline is out of scope for the audio work.
"""
from __future__ import annotations

import asyncio
import os
import weakref

DEFAULT_ELEVENLABS_MAX_CONCURRENCY = 10
DEFAULT_THREAD_POOL_SIZE = 32
DEFAULT_AUDIO_MAX_CONCURRENT_JOBS = 3

_elevenlabs_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def elevenlabs_max_concurrency() -> int:
    return _positive_env_int("ELEVENLABS_MAX_CONCURRENCY", DEFAULT_ELEVENLABS_MAX_CONCURRENCY)


def audio_max_concurrent_jobs() -> int:
    """AUDIO_MAX_CONCURRENT_JOBS, or 3: how many audio jobs (Excel and story)
    run at once. The rest wait in the job queue."""
    return _positive_env_int("AUDIO_MAX_CONCURRENT_JOBS", DEFAULT_AUDIO_MAX_CONCURRENT_JOBS)


def thread_pool_size() -> int:
    """APP_THREAD_POOL_SIZE, or 32. TTS, translation, compression and S3 calls
    all run in the default executor; the platform default (cores + 4) is too
    tight once a row's languages are spoken concurrently across several jobs."""
    return _positive_env_int("APP_THREAD_POOL_SIZE", DEFAULT_THREAD_POOL_SIZE)


def elevenlabs_limit() -> asyncio.Semaphore:
    """The running loop's ElevenLabs semaphore. Call from inside a coroutine."""
    loop = asyncio.get_running_loop()
    semaphore = _elevenlabs_by_loop.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(elevenlabs_max_concurrency())
        _elevenlabs_by_loop[loop] = semaphore
    return semaphore


# The folder upload lock: one asyncio.Lock per S3 activity folder, per loop.
# Held around one job's whole per-activity upload (batch/upload.py) so two jobs
# writing to the same folder take turns. Append mode is a read-modify-write of
# each language zip, so without it the last writer silently drops the other
# job's files. The inner dict holds its locks weakly: a lock exists only while
# some upload holds it or waits for it, so idle folders don't pile up.
_folder_locks_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def folder_upload_lock(folder_name: str) -> asyncio.Lock:
    """The running loop's lock for one S3 activity folder. Call from inside a
    coroutine and keep a reference for as long as you hold or wait on it
    (``async with folder_upload_lock(name):`` does)."""
    loop = asyncio.get_running_loop()
    locks = _folder_locks_by_loop.get(loop)
    if locks is None:
        locks = weakref.WeakValueDictionary()
        _folder_locks_by_loop[loop] = locks
    lock = locks.get(folder_name)
    if lock is None:
        lock = asyncio.Lock()
        locks[folder_name] = lock
    return lock
