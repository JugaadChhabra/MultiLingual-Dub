from __future__ import annotations

import os
import threading
import time

from deep_translator import GoogleTranslator

from services.retry import retry_call

FREE_TRANSLATE_LANGUAGES = {"fr", "de", "es", "ru", "pt"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class _RateLimiter:
    """Process-wide, thread-safe pacer that spaces calls evenly.

    Google's free translate endpoint caps a single IP at ~5 req/sec. Every
    free-translate call runs in its own worker thread (via asyncio.to_thread),
    so without a shared limiter our own concurrency is what trips the 429s.
    Each acquirer reserves the next evenly-spaced slot under the lock and then
    sleeps to it outside the lock, so N threads never exceed the target rate.
    """

    def __init__(self, rate_per_sec: float) -> None:
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed)
            self._next_allowed = scheduled + self._min_interval
            wait = scheduled - now
        if wait > 0:
            time.sleep(wait)


# Default to 4 req/sec to stay comfortably under Google's ~5 req/sec IP cap.
_LIMITER = _RateLimiter(_env_float("FREE_TRANSLATE_MAX_RPS", 4.0))


def normalize_language_code(language_code: str) -> str:
    normalized = language_code.strip().lower().replace("_", "-")
    if not normalized:
        return ""
    return normalized.split("-", 1)[0]


def should_use_free_translate(target_language_code: str) -> bool:
    return normalize_language_code(target_language_code) in FREE_TRANSLATE_LANGUAGES


def translate_text_free(
    text: str,
    target_language_code: str,
    *,
    source_language_code: str = "auto",
) -> str:

    normalized_target = normalize_language_code(target_language_code)
    normalized_source = normalize_language_code(source_language_code)

    if normalized_source and normalized_source != "auto" and normalized_source == normalized_target:
        return text

    if not normalized_target:
        raise ValueError("Target language code cannot be empty")

    if normalized_target not in FREE_TRANSLATE_LANGUAGES:
        raise ValueError(
            f"Unsupported in-process free translation target language: {normalized_target}"
        )

    def _call_once() -> str:
        _LIMITER.acquire()
        translated = GoogleTranslator(source="auto", target=normalized_target).translate(text)
        if not isinstance(translated, str) or not translated.strip():
            raise RuntimeError("In-process free translator returned empty translation")
        return translated

    # Keep this independent of the global API_RETRY_MAX_ATTEMPTS: against a
    # per-IP quota, retrying harder just keeps us pinned at the limit, so cap
    # attempts low and let a genuinely-blocked job fail fast.
    return retry_call(
        _call_once,
        operation="in-process free translate",
        max_attempts=_env_int("FREE_TRANSLATE_MAX_ATTEMPTS", 5),
    )
