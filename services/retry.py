from __future__ import annotations

import logging
import os
import random
import time
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _status_code_from_exc(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _is_same_language_error_message(message: str) -> bool:
    text = message.strip().lower()
    if not text:
        return False

    has_source_target = "source" in text and "target" in text
    has_language = "language" in text or "lang" in text
    has_same_or_different = "same" in text or "different" in text
    return has_source_target and has_language and has_same_or_different


def is_retryable(exc: Exception) -> bool:
    status = _status_code_from_exc(exc)
    if status is not None:
        if status == 429 or status >= 500:
            return True
        return False

    message = str(exc).lower()
    if _is_same_language_error_message(message):
        return False

    retry_markers = (
        "rate limit",
        "429",
        "timeout",
        "timed out",
        "temporarily",
        "try again",
        "connection reset",
        "connection aborted",
        "connection error",
        "connection closed",
        "server disconnected without sending a response",
        "peer closed connection without sending complete message body",
        "incomplete message body",
        "remote protocol error",
        "remoteprotocolerror",
        "service unavailable",
        "unavailable",
        "502",
        "503",
        "504",
        "gateway timeout",
        "too many requests",
    )
    return any(marker in message for marker in retry_markers)


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None

    raw = str(raw).strip()
    if not raw:
        return None

    # Retry-After can be seconds (preferred) or an HTTP date.
    try:
        seconds = float(raw)
        if seconds >= 0:
            return seconds
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            return None
        delay = retry_at.timestamp() - time.time()
        return max(0.0, delay)
    except (TypeError, ValueError, OverflowError):
        return None


def retry_call(
    func: Callable[[], T],
    *,
    max_attempts: int | None = None,
    base_delay_s: float | None = None,
    max_delay_s: float | None = None,
    operation: str | None = None,
) -> T:
    attempts = max_attempts if max_attempts is not None else _env_int("API_RETRY_MAX_ATTEMPTS", 5)
    base_delay = base_delay_s if base_delay_s is not None else _env_float("API_RETRY_BASE_DELAY_S", 0.5)
    max_delay = max_delay_s if max_delay_s is not None else _env_float("API_RETRY_MAX_DELAY_S", 8.0)
    rate_limit_base_delay = _env_float(
        "API_RETRY_RATE_LIMIT_BASE_DELAY_S",
        max(1.0, base_delay * 2),
    )
    rate_limit_max_delay = _env_float(
        "API_RETRY_RATE_LIMIT_MAX_DELAY_S",
        max(30.0, max_delay),
    )
    op_label = operation or "operation"

    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            should_retry = is_retryable(exc)
            if attempt >= attempts or not should_retry:
                raise

            status = _status_code_from_exc(exc)
            if status == 429:
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    sleep_for = min(rate_limit_max_delay, retry_after)
                else:
                    delay = min(rate_limit_max_delay, rate_limit_base_delay * (2 ** (attempt - 1)))
                    jitter = random.uniform(0, delay * 0.25)
                    sleep_for = min(rate_limit_max_delay, delay + jitter)
            else:
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                jitter = random.uniform(0, delay * 0.25)
                sleep_for = min(max_delay, delay + jitter)

            logger.warning(
                "%s failed (attempt %d/%d): %s; retrying in %.2fs",
                op_label,
                attempt,
                attempts,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)

    return func()
