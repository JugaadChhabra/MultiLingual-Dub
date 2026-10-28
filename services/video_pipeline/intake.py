"""Video job intake: a single submission's raw form values in, a validated spec out.

The Video pane scrubs the title and caps the script before it sends, but the pane
is only the fast path for feedback. The title becomes the NAS filename and a
script over the writer's limit is one HeyGen will not render, so the server
enforces the same rules here rather than trusting whoever sent the request.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from services.script_writer import MAX_SCRIPT_CHARS
from services.video_pipeline.types import VideoJobSpec

# Same character set as safeTitle / scrubTitle in static/pane-video.js: a run of
# them becomes one underscore, then a trailing .mp4 is dropped because the NAS
# path adds its own.
_UNSAFE_TITLE = re.compile(r'[\\/:*?"<>|]+')
_MP4_SUFFIX = re.compile(r"\.mp4$", re.IGNORECASE)

# What FastAPI's bool form fields accepted before intake owned the parsing, so a
# caller that worked then still works.
_TRUE = {"1", "true", "t", "on", "yes", "y"}
_FALSE = {"0", "false", "f", "off", "no", "n"}


class IntakeError(ValueError):
    """A submission that cannot become a video job. The message is for the operator."""


@dataclass(frozen=True)
class SingleSubmission:
    """The single-video form as it arrived: strings as typed, numbers as parsed.

    The upload is not here — whether there is a photo is the request's business,
    not the spec's.
    """

    script: str
    video_title: str | None = None
    character: str | None = None
    voice_id: str | None = None
    motion_prompt: str | None = None
    publish_date: str | None = None
    talking_photo_id: str | None = None
    width: int | None = None
    height: int | None = None
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: str | None = None
    include_overlay: str | None = None
    include_music: str | None = None


def spec_from_single(fields: SingleSubmission) -> VideoJobSpec:
    script = fields.script.strip()
    if not script:
        raise IntakeError("script must not be empty")
    if len(script) > MAX_SCRIPT_CHARS:
        raise IntakeError(
            f"script is {len(script)} characters; the limit is {MAX_SCRIPT_CHARS}"
        )

    # A blank title keeps the spec default rather than filing a video as ".mp4".
    title = _scrub_title(fields.video_title or "")
    optional_title = {"video_title": title} if title else {}

    return VideoJobSpec(
        script=script,
        character=_blank_to_none(fields.character) or "indian",
        voice_id=_blank_to_none(fields.voice_id),
        motion_prompt=_blank_to_none(fields.motion_prompt),
        publish_date=_blank_to_none(fields.publish_date),
        talking_photo_id=_blank_to_none(fields.talking_photo_id),
        width=fields.width,
        height=fields.height,
        stability=fields.stability,
        similarity_boost=fields.similarity_boost,
        style=fields.style,
        use_speaker_boost=_toggle("use_speaker_boost", fields.use_speaker_boost),
        include_overlay=_toggle("include_overlay", fields.include_overlay),
        include_music=_toggle("include_music", fields.include_music),
        **optional_title,
    )


def _scrub_title(raw: str) -> str:
    return _MP4_SUFFIX.sub("", _UNSAFE_TITLE.sub("_", raw.strip()))


def _blank_to_none(value: str | None) -> str | None:
    return value if value and value.strip() else None


def _toggle(name: str, raw: str | None) -> bool:
    """Absent or blank means on: every toggle defaults on, as the spec does."""
    value = (raw or "").strip().lower()
    if not value or value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise IntakeError(f"{name} must be true or false, not {raw!r}")
