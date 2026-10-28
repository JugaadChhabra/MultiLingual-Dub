"""Video job intake: a submission's raw form values in, validated specs out.

A single submission becomes one spec; a batch (rows from Excel or JSON plus the
settings every row shares) becomes one spec per row, under the same rules.

The Video pane scrubs the title and caps the script before it sends, but the pane
is only the fast path for feedback. The title becomes the NAS filename and a
script over the writer's limit is one HeyGen will not render, so the server
enforces the same rules here rather than trusting whoever sent the request.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from services.script_writer import MAX_SCRIPT_CHARS
from services.video_pipeline.batch_excel import HeyGenBatchRow
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


# A refused batch lists its problems one per line; past this many the rest are
# counted, so a spreadsheet gone wrong throughout stays readable.
_MAX_PROBLEM_LINES = 20


class IntakeError(ValueError):
    """A submission that cannot become a video job. The message is for the operator.

    ``problems`` holds each problem on its own; the message is the whole of them
    (capped for a batch), so whoever shows ``str(exc)`` shows everything.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems if problems is not None else [message]


@dataclass(frozen=True)
class SingleSubmission:
    """The single-video form as it arrived: strings as typed, numbers as parsed.

    The upload is not here — whether there is a photo is the request's business,
    not the spec's.
    """

    script: str
    video_title: str
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

    # The title is the NAS filename. One that scrubs to nothing would be filed as
    # ".mp4" — and falling back to the shared default would overwrite the day's
    # previous render — so it is refused, as the pane refuses it.
    title = _scrub_title(fields.video_title)
    if not title:
        raise IntakeError("video_title must name the file so it does not overwrite an earlier render")

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
        video_title=title,
    )


@dataclass(frozen=True)
class BatchSettings:
    """What every row of a batch shares, as it arrived on the form."""

    character: str | None = None
    motion_prompt: str | None = None
    publish_date: str | None = None
    include_overlay: str | None = None
    include_music: str | None = None


@dataclass(frozen=True)
class BatchItem:
    """One row's spec, still tied to the row it came from so the batch store's
    per-row state can follow it."""

    row_index: int
    spec: VideoJobSpec


def specs_from_batch(rows: list[HeyGenBatchRow], shared: BatchSettings) -> list[BatchItem]:
    """Every row is checked before the batch is refused, so one refusal names
    every bad row and a single fix-up pass is enough. Any problem still refuses
    the whole batch: renders are paid for at submission."""
    # A bad shared toggle is wrong for every row alike; said once, not per row.
    _toggle("include_overlay", shared.include_overlay)
    _toggle("include_music", shared.include_music)

    items: list[BatchItem] = []
    problems: list[str] = []
    titles: list[str] = []
    for row in rows:
        # A row refused for its script still names a file once that is fixed,
        # so its title takes part in the duplicate check.
        title = _scrub_title(row.video_title)
        if title:
            titles.append(title)
        try:
            spec = spec_from_single(SingleSubmission(
                script=row.script,
                video_title=row.video_title,
                character=shared.character,
                motion_prompt=shared.motion_prompt,
                publish_date=shared.publish_date,
                include_overlay=shared.include_overlay,
                include_music=shared.include_music,
            ))
        except IntakeError as exc:
            # The index is the spreadsheet row; the title is what an Auto
            # operator recognises, since their drafts have no row numbers.
            problems.append(f"row {row.row_index} ({row.video_title!r}): {exc}")
            continue
        items.append(BatchItem(row_index=row.row_index, spec=spec))

    # Each title is a file in the same NAS folder, so two rows with one title
    # means one render overwrites the other. Compared after scrubbing, and
    # without case, as the share and the pane compare them.
    counts = Counter(t.casefold() for t in titles)
    dupes = sorted({t for t in titles if counts[t.casefold()] > 1})
    if dupes:
        problems.append(
            "video titles must be unique, or one render overwrites another: "
            + ", ".join(dupes)
        )

    if problems:
        shown = problems[:_MAX_PROBLEM_LINES]
        if len(problems) > _MAX_PROBLEM_LINES:
            shown.append(f"…and {len(problems) - _MAX_PROBLEM_LINES} more")
        raise IntakeError("\n".join(shown), problems=problems)
    return items


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
