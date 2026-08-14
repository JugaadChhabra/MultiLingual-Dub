from __future__ import annotations

import uuid


def _sanitize_for_key(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw.strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "na"


def _build_s3_key(job_id: str, target_language: str, audio_type: str) -> str:
    audio_type_fragment = _sanitize_for_key(audio_type)
    return f"batch/{job_id}/{target_language}/{audio_type_fragment}-{uuid.uuid4().hex}.mp3"


def _resolve_activity_segment_name(raw_activity_name: str, current_activity_name: str | None) -> str:
    if raw_activity_name.strip():
        return _sanitize_for_key(raw_activity_name)
    if current_activity_name:
        return current_activity_name
    return "batch"


def _next_activity_folder_name(activity_name: str, upload_counts: dict[str, int]) -> str:
    next_count = upload_counts.get(activity_name, 0) + 1
    upload_counts[activity_name] = next_count
    if next_count == 1:
        return activity_name
    return f"{activity_name}-{next_count}"
