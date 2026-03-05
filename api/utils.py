from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException, UploadFile


def safe_stem(filename: str) -> str:
    return Path(filename).stem.replace(" ", "_")


def ensure_file_extension(filename: str | None, expected_suffix: str, detail: str) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="Missing file name")
    if Path(filename).suffix.lower() != expected_suffix:
        raise HTTPException(status_code=400, detail=detail)
    return filename


def to_output_url(file_path: str | Path, output_dir: Path) -> str | None:
    path = Path(file_path)
    try:
        rel_path = path.resolve().relative_to(output_dir.resolve())
    except ValueError:
        return None
    if not path.exists():
        return None
    return f"/output/{rel_path.as_posix()}"


async def save_upload_file(upload: UploadFile, destination: Path) -> None:
    try:
        payload = await upload.read()
        destination.write_bytes(payload)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to save upload") from exc


def parse_target_languages(raw_items: list[str] | None, raw_json: str | None) -> list[str]:
    candidates: list[str] = []

    if raw_items:
        candidates.extend(raw_items)

    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="target_languages_json must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise HTTPException(status_code=400, detail="target_languages_json must be a JSON array")
        candidates.extend(str(item) for item in parsed)

    # Handle case where a single item is itself a JSON array string
    if len(candidates) == 1 and candidates[0].strip().startswith("[") and candidates[0].strip().endswith("]"):
        try:
            parsed = json.loads(candidates[0])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            candidates = [str(item) for item in parsed]

    deduped = _dedup_languages(candidates)
    if not deduped:
        raise HTTPException(status_code=400, detail="At least one target language is required")
    return deduped


def _dedup_languages(candidates: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        lang = item.strip()
        if lang and lang not in seen:
            result.append(lang)
            seen.add(lang)
    return result
