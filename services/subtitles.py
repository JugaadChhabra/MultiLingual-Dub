from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _coerce_seconds(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _resolve_segment_bounds_ms(segment: dict[str, Any]) -> tuple[int | None, int | None]:
    start_s = _coerce_seconds(segment.get("start"))
    if start_s is None:
        start_s = _coerce_seconds(segment.get("start_time"))

    end_s = _coerce_seconds(segment.get("end"))
    if end_s is None:
        end_s = _coerce_seconds(segment.get("end_time"))

    duration_s = _coerce_seconds(segment.get("duration"))

    if end_s is None and start_s is not None and duration_s is not None:
        end_s = start_s + max(0.0, duration_s)
    if start_s is None and end_s is not None and duration_s is not None:
        start_s = max(0.0, end_s - duration_s)

    if start_s is None or end_s is None or end_s <= start_s:
        return None, None

    return int(round(start_s * 1000)), int(round(end_s * 1000))


def _split_words(text: str, char_limit: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= char_limit:
            current = candidate
            continue
        chunks.append(current)
        current = word
    chunks.append(current)
    return chunks


def _wrap_subtitle_text(text: str, max_chars_per_line: int) -> str:
    lines = _split_words(text, max_chars_per_line)
    if not lines:
        return text
    return "\n".join(lines)


def _split_text_for_cues(
    text: str,
    *,
    max_chars_per_line: int,
    max_lines: int,
) -> list[str]:
    total_limit = max_chars_per_line * max_lines
    chunks = _split_words(text, total_limit)
    if not chunks:
        return []
    return [_wrap_subtitle_text(chunk, max_chars_per_line) for chunk in chunks]


def _estimated_duration_ms(text: str) -> int:
    # Roughly 15 chars/sec with lower and upper bounds.
    chars = max(1, len(text.replace("\n", " ").strip()))
    return max(1200, min(5000, int(chars * 1000 / 15)))


def _cues_from_timed_segment(
    text: str,
    start_ms: int,
    end_ms: int,
    *,
    max_chars_per_line: int,
    max_lines: int,
) -> list[SubtitleCue]:
    pieces = _split_text_for_cues(
        text,
        max_chars_per_line=max_chars_per_line,
        max_lines=max_lines,
    )
    if not pieces:
        return []

    segment_duration = max(1, end_ms - start_ms)
    piece_duration = max(1, segment_duration // len(pieces))

    cues: list[SubtitleCue] = []
    cursor = start_ms
    for idx, piece in enumerate(pieces):
        piece_end = end_ms if idx == len(pieces) - 1 else min(end_ms, cursor + piece_duration)
        if piece_end <= cursor:
            piece_end = cursor + 1
        cues.append(SubtitleCue(start_ms=cursor, end_ms=piece_end, text=piece))
        cursor = piece_end

    return cues


def _cues_from_plain_text(
    text: str,
    *,
    max_chars_per_line: int,
    max_lines: int,
    start_ms: int = 0,
) -> list[SubtitleCue]:
    pieces = _split_text_for_cues(
        text,
        max_chars_per_line=max_chars_per_line,
        max_lines=max_lines,
    )
    cues: list[SubtitleCue] = []
    cursor = max(0, start_ms)
    for piece in pieces:
        duration_ms = _estimated_duration_ms(piece)
        cues.append(SubtitleCue(start_ms=cursor, end_ms=cursor + duration_ms, text=piece))
        cursor += duration_ms + 80
    return cues


def _extract_transcript(payload: dict[str, Any]) -> str:
    for key in ("text", "transcript", "transcription"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_text(value)

    raw_segments = payload.get("segments")
    if isinstance(raw_segments, list):
        parts = []
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            value = segment.get("text") or segment.get("transcript")
            if isinstance(value, str) and value.strip():
                parts.append(_normalize_text(value))
        if parts:
            return " ".join(parts)

    return ""


def _extract_raw_segments(payload: dict[str, Any]) -> list[tuple[str, int | None, int | None]]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return []

    extracted: list[tuple[str, int | None, int | None]] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        raw_text = segment.get("text") or segment.get("transcript")
        if not isinstance(raw_text, str):
            continue
        text = _normalize_text(raw_text)
        if not text:
            continue

        start_ms, end_ms = _resolve_segment_bounds_ms(segment)
        extracted.append((text, start_ms, end_ms))
    return extracted


def _find_stt_json_path(output_dir: Path, source_audio_filename: str) -> Path | None:
    audio_name = Path(source_audio_filename).name
    audio_stem = Path(audio_name).stem
    candidates = (
        output_dir / f"{audio_stem}.json",
        output_dir / f"{audio_name}.json",
        output_dir / f"{audio_name}.mp3.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _format_srt_timestamp(ms: int) -> str:
    total_ms = max(0, ms)
    hours = total_ms // 3_600_000
    remainder = total_ms % 3_600_000
    minutes = remainder // 60_000
    remainder = remainder % 60_000
    seconds = remainder // 1000
    millis = remainder % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt(cues: list[SubtitleCue]) -> str:
    blocks: list[str] = []
    for idx, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(idx),
                    f"{_format_srt_timestamp(cue.start_ms)} --> {_format_srt_timestamp(cue.end_ms)}",
                    cue.text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def cue_texts(cues: list[SubtitleCue]) -> list[str]:
    return [cue.text for cue in cues]


def cues_with_replaced_text(cues: list[SubtitleCue], texts: list[str]) -> list[SubtitleCue]:
    if len(cues) != len(texts):
        raise ValueError("Cue/text length mismatch")

    result: list[SubtitleCue] = []
    for cue, text in zip(cues, texts):
        normalized = _normalize_text(text)
        result.append(
            SubtitleCue(
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                text=normalized if normalized else cue.text,
            )
        )
    return result


def write_srt_file(output_path: Path, cues: list[SubtitleCue]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_srt(cues), encoding="utf-8")
    return output_path


def build_srt_for_audio_file(
    *,
    output_dir: Path,
    source_audio_filename: str,
    fallback_transcript: str,
    output_filename: str | None = None,
    max_chars_per_line: int = 42,
    max_lines: int = 2,
) -> tuple[Path, list[SubtitleCue]]:
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript = _normalize_text(fallback_transcript)
    cues: list[SubtitleCue] = []

    json_path = _find_stt_json_path(output_dir, source_audio_filename)
    if json_path is not None:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None

        if isinstance(payload, dict):
            if not transcript:
                transcript = _extract_transcript(payload)

            raw_segments = _extract_raw_segments(payload)
            cursor = 0
            for text, start_ms, end_ms in raw_segments:
                effective_start = cursor if start_ms is None else max(cursor, start_ms)
                effective_end = end_ms
                if effective_end is None or effective_end <= effective_start:
                    effective_end = effective_start + _estimated_duration_ms(text)

                segment_cues = _cues_from_timed_segment(
                    text,
                    effective_start,
                    effective_end,
                    max_chars_per_line=max_chars_per_line,
                    max_lines=max_lines,
                )
                cues.extend(segment_cues)
                if segment_cues:
                    cursor = segment_cues[-1].end_ms + 80

    if not cues and transcript:
        cues = _cues_from_plain_text(
            transcript,
            max_chars_per_line=max_chars_per_line,
            max_lines=max_lines,
            start_ms=0,
        )

    if not cues:
        raise RuntimeError("Unable to build subtitle cues from STT output")

    out_name = output_filename or f"{Path(source_audio_filename).stem}.srt"
    output_path = output_dir / out_name
    return write_srt_file(output_path, cues), cues
