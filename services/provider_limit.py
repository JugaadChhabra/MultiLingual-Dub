"""The provider limit: a process-wide cap on concurrent ElevenLabs calls.

Every audio job (Excel and story) speaks through ``speak_row``, which holds a
slot here around each language's TTS call, retries included. So however many
jobs run at once, ElevenLabs never sees more than ``ELEVENLABS_MAX_CONCURRENCY``
calls from this process (default 10, the Pro plan's limit). A call backing off
after a 429 keeps its slot; a 429 means we should be sending less anyway.

Read straight from the environment, like ``API_RETRY_*``: it describes the
account's plan as deployed, not a per-session setting.

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

_elevenlabs_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def elevenlabs_max_concurrency() -> int:
    raw = os.getenv("ELEVENLABS_MAX_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_ELEVENLABS_MAX_CONCURRENCY
    except ValueError:
        value = DEFAULT_ELEVENLABS_MAX_CONCURRENCY
    return value if value > 0 else DEFAULT_ELEVENLABS_MAX_CONCURRENCY


def elevenlabs_limit() -> asyncio.Semaphore:
    """The running loop's ElevenLabs semaphore. Call from inside a coroutine."""
    loop = asyncio.get_running_loop()
    semaphore = _elevenlabs_by_loop.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(elevenlabs_max_concurrency())
        _elevenlabs_by_loop[loop] = semaphore
    return semaphore
