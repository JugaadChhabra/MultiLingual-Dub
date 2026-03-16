from __future__ import annotations

import logging
import os
import random
import time
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


def is_retryable(exc: Exception) -> bool:
    status = _status_code_from_exc(exc)
    if status is not None:
        if status == 429 or status >= 500:
            return True
        return False

    message = str(exc).lower()
    if "source and target languages must be different" in message:
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
        "service unavailable",
        "unavailable",
        "502",
        "503",
        "504",
        "gateway timeout",
        "too many requests",
    )
    return any(marker in message for marker in retry_markers)


def retry_call(
    func: Callable[[], T],
    *,
    max_attempts: int | None = None,
    base_delay_s: float | None = None,
    max_delay_s: float | None = None,
    operation: str | None = None,
) -> T:
    attempts = max_attempts or _env_int("API_RETRY_MAX_ATTEMPTS", 5)
    base_delay = base_delay_s or _env_float("API_RETRY_BASE_DELAY_S", 0.5)
    max_delay = max_delay_s or _env_float("API_RETRY_MAX_DELAY_S", 8.0)
    op_label = operation or "operation"

    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            should_retry = is_retryable(exc)
            if attempt >= attempts or not should_retry:
                raise

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
