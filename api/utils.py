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


UPLOAD_CHUNK_BYTES = 1024 * 1024


async def save_upload_file(upload: UploadFile, destination: Path, *, max_mb: float | None = None) -> None:
    """Copy an upload to ``destination`` a chunk at a time, never holding the
    whole body in memory. Past ``max_mb`` (None: no cap) it stops and raises
    413. On any failure the partial file is deleted."""
    limit = int(max_mb * 1024 * 1024) if max_mb is not None else None
    written = 0
    try:
        with destination.open("wb") as out:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if limit is not None and written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"This file is over the {max_mb:g} MB upload limit.",
                    )
                out.write(chunk)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to save upload") from exc
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


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
