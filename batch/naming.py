from __future__ import annotations

import uuid
from pathlib import Path


def _sanitize_for_key(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw.strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "na"


def _build_s3_key(job_id: str, target_language: str, audio_type: str) -> str:
    audio_type_fragment = _sanitize_for_key(audio_type)
    return f"batch/{job_id}/{target_language}/{audio_type_fragment}-{uuid.uuid4().hex}.mp3"


def _dedupe_filename(
    filename: str,
    existing_files: dict[str, bytes],
    row_index: int,
) -> tuple[str, bool]:
    if filename not in existing_files:
        return filename, False

    stem = Path(filename).stem or "audio"
    suffix = Path(filename).suffix or ".mp3"
    candidate = f"{stem}-row{row_index}{suffix}"
    counter = 2
    while candidate in existing_files:
        candidate = f"{stem}-row{row_index}-{counter}{suffix}"
        counter += 1
    return candidate, True


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


def _new_language_audio_buffers(target_languages: list[str]) -> dict[str, dict[str, bytes]]:
    return {lang: {} for lang in target_languages}


def _build_output_filename(*, audio_type: str, row_index: int, language: str) -> str:
    filename = audio_type or f"row-{row_index}-{language}"
    if not filename.lower().endswith(".mp3"):
        filename = f"{filename}.mp3"
    return filename
