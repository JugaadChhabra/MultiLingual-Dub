from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock

_IMPORTANT_LOG_BUFFER: deque[dict[str, object]] = deque(maxlen=400)
_IMPORTANT_LOG_LOCK = Lock()
_IMPORTANT_LOG_ID = 0

# INFO logs from these loggers are shown only if they match _IMPORTANT_MSG_MARKERS.
# WARNING+ from any logger always passes through.
_INFO_LOGGER_PREFIXES = (
    "batch",
    "services.qc",
    "services.elevenlabs",
    "api.routes",
)

# Only INFO messages containing one of these substrings make it to the UI.
# This filters out per-row/per-language noise like "TTS start", "translating into",
# "ready for zip", "QC start", keeping only lifecycle events users care about.
_IMPORTANT_MSG_MARKERS = (
    "started",
    "completed",
    "failed",
    "crashed",
    "cancel",
    "running",
    "uploaded zip",
    "upload failed",
    "retry",
    "rows x",
    "row complete",
    "config error",
    "setup failed",
)


def _is_important_log_record(record: logging.LogRecord) -> bool:
    if record.levelno >= logging.WARNING:
        return True
    if record.levelno >= logging.INFO:
        name = record.name or ""
        if any(name.startswith(prefix) for prefix in _INFO_LOGGER_PREFIXES):
            msg = record.getMessage().lower()
            return any(marker in msg for marker in _IMPORTANT_MSG_MARKERS)
    return False


class ImportantLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _IMPORTANT_LOG_ID
        if not _is_important_log_record(record):
            return
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with _IMPORTANT_LOG_LOCK:
            _IMPORTANT_LOG_ID += 1
            payload["id"] = _IMPORTANT_LOG_ID
            _IMPORTANT_LOG_BUFFER.append(payload)


def install_log_handler() -> None:
    root_logger = logging.getLogger()
    if not any(isinstance(h, ImportantLogHandler) for h in root_logger.handlers):
        handler = ImportantLogHandler()
        handler.setLevel(logging.INFO)
        root_logger.addHandler(handler)


def get_important_logs(since_id: int = 0, limit: int = 200) -> dict:
    safe_limit = max(1, min(limit, 400))
    safe_since = max(0, since_id)
    with _IMPORTANT_LOG_LOCK:
        items = [item for item in _IMPORTANT_LOG_BUFFER if int(item.get("id", 0)) > safe_since]
        if len(items) > safe_limit:
            items = items[-safe_limit:]
    latest_id = items[-1]["id"] if items else safe_since
    return {"logs": items, "latest_id": latest_id}
