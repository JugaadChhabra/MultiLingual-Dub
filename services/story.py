"""Story mode: a pasted story broken into voiceover lines, each with an emotion.

Gemini does the splitting, under the same settings, retry helper and model
fallback as per-row QC. What it returns is never trusted: a split is accepted
only if it keeps the operator's wording, uses emotions from ``STORY_EMOTIONS``
and keeps every line under ``MAX_LINE_CHARS``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import google.genai as genai
from google.genai import types

from services.qc import QCSettings, _parse_response_json
from services.retry import retry_call

logger = logging.getLogger(__name__)

# The emotions a line may carry. Each becomes an ElevenLabs audio tag, so this
# one list is both what Gemini may pick from and what the review dropdown offers.
STORY_EMOTIONS = [
    "Cheerful", "Narrative", "Curious", "Clever", "Calm", "Grateful", "Worried",
    "Energetic", "Joyful", "Thoughtful", "Magical", "Determined", "Inspirational",
    "Sad", "Excited", "Surprised", "Scared", "Gentle",
]

# Counted with the "[Emotion] " tag the line is voiced with, and set well under
# ElevenLabs' per-request limit.
MAX_LINE_CHARS = 800

# One story is one run. Longer ones are refused rather than chunked.
MAX_STORY_LINES = 120

# A split that fails validation gets one more Gemini call before it is refused.
_SPLIT_ATTEMPTS = 2


class StorySplitError(Exception):
    """The story, or Gemini's split of it, cannot be used. The operator's to fix."""


class StoryTooLongError(StorySplitError):
    """More lines than one run takes. Final: retrying will not shorten it."""


class StoryGeminiError(Exception):
    """Gemini could not be reached on any model."""


@dataclass(frozen=True)
class StoryLine:
    text: str
    emotion: str


def activity_name(title: str) -> str:
    """The pre-filled activity name: "The boy who…" → "theboywho…story"."""
    return re.sub(r"[^a-z0-9]", "", title.lower()) + "story"


def line_problem(line: StoryLine) -> str | None:
    """Why this line cannot be voiced as it is, or None."""
    if not line.text.strip():
        return "a line is empty"
    if line.emotion not in STORY_EMOTIONS:
        return f"emotion {line.emotion!r} is not one of {', '.join(STORY_EMOTIONS)}"
    tagged = len(f"[{line.emotion}] {line.text}")
    if tagged > MAX_LINE_CHARS:
        return f"a line is {tagged} characters with its emotion tag; the limit is {MAX_LINE_CHARS}"
    return None


_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"'})


def _normalise(text: str) -> str:
    return " ".join(text.translate(_QUOTES).split())


_SYSTEM_INSTRUCTION = f"""You split children's stories into voiceover lines. Each line is recorded as a separate audio clip and read aloud with an emotion.

Rules:
1) NEVER change the wording. Do not add, remove, fix or reorder a single word or punctuation mark. Joining your lines with spaces must give back the story exactly.
2) Each line is 1 to 3 sentences, usually 1 or 2. Short dramatic sentences may stand alone.
3) A quotation stays on one line together with its "said X" and any speech around it.
4) The opening greeting gets its own line. The closing moral ("So, from this story, we learn…") gets its own line.
5) No line may be longer than {MAX_LINE_CHARS - 20} characters.
6) Give each line exactly one emotion, chosen ONLY from this list: {", ".join(STORY_EMOTIONS)}. Use Narrative for plain storytelling; pick another only when the line clearly carries that feeling.
7) Output valid JSON only (no markdown, no code fences, no commentary), in this shape:
{{"lines": [{{"text": "...", "emotion": "..."}}]}}"""


def _parse_lines(response_text: str) -> list[StoryLine]:
    try:
        payload = _parse_response_json(response_text)
    except (ValueError, TypeError) as exc:
        raise StorySplitError(f"Gemini did not return JSON: {exc}") from exc
    items = payload.get("lines") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise StorySplitError("Gemini's reply has no lines")
    lines = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise StorySplitError("Gemini's reply has a line without text")
        lines.append(StoryLine(text=item["text"].strip(), emotion=str(item.get("emotion", "")).strip()))
    return lines


def _validate(source: str, lines: list[StoryLine]) -> None:
    """Raise StorySplitError if this split cannot be accepted."""
    for line in lines:
        problem = line_problem(line)
        if problem:
            raise StorySplitError(f"The split was rejected: {problem}")
    if _normalise(" ".join(line.text for line in lines)) != _normalise(source):
        raise StorySplitError("The split was rejected: it changed the story's wording")


def _too_long() -> StoryTooLongError:
    return StoryTooLongError(
        f"This story is too long for one run: the limit is {MAX_STORY_LINES} lines. "
        "Split it into parts and run each one."
    )


def _generate(client, models: list[str], prompt: str, config) -> str:
    last_exc: Exception | None = None
    for model in models:
        try:
            response = retry_call(
                lambda: client.models.generate_content(model=model, contents=prompt, config=config),
                operation=f"Gemini story split ({model})",
            )
            return response.text.strip()
        except Exception as exc:
            last_exc = exc
            logger.warning("Story split: model %s failed: %s", model, exc)
    raise StoryGeminiError(f"Gemini story split failed after {len(models)} models: {last_exc}")


def split_story(text: str, *, settings: QCSettings) -> list[StoryLine]:
    """Split a story into voiceover lines with emotions, keeping its wording.

    Raises StorySplitError for an empty or over-size story, or a split that
    still fails validation after one retry; StoryGeminiError if no model answers.
    """
    source = text.strip()
    if not source:
        raise StorySplitError("Paste a story first: the text is empty")
    # No split of this can fit, so it is refused before Gemini is paid to try.
    if len(_normalise(source)) > MAX_STORY_LINES * MAX_LINE_CHARS:
        raise _too_long()

    client = genai.Client(api_key=settings.api_key)
    config = types.GenerateContentConfig(system_instruction=_SYSTEM_INSTRUCTION)
    prompt = f"Story:\n{source}"

    last_error: StorySplitError | None = None
    for attempt in range(1, _SPLIT_ATTEMPTS + 1):
        reply = _generate(client, settings.models, prompt, config)
        try:
            lines = _parse_lines(reply)
            if len(lines) > MAX_STORY_LINES:
                raise _too_long()
            _validate(source, lines)
        except StoryTooLongError:
            raise
        except StorySplitError as exc:
            last_error = exc
            logger.warning("Story split attempt %d rejected: %s", attempt, exc)
            continue
        logger.info("Story split into %d lines", len(lines))
        return lines

    assert last_error is not None
    raise last_error
