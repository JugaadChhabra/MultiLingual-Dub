"""Story mode: a pasted story broken into voiceover lines, each with an emotion.

Gemini does the splitting, under the same settings, retry helper and model
fallback as per-row QC. What it returns is never trusted: a split is accepted
only if it keeps the operator's wording, uses emotions from ``STORY_EMOTIONS``
and keeps every line under ``MAX_LINE_CHARS``.

Gemini also does the *story fix*: one call per language that sees every line's
English beside its literal draft and rewrites the drafts so the story sounds
told, not translated.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass

import google.genai as genai
from google.genai import types

from services.languages import LANGUAGE_NAMES, LANGUAGE_SCRIPT_HINTS
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


class StoryGeminiError(Exception):
    """Gemini could not be reached on any model."""


class StoryFixError(Exception):
    """No model returned a usable story fix for a language."""


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
    # NFKC folds typography a model rewrites without meaning to: "…" to "...",
    # composed and decomposed accents to one form.
    return " ".join(unicodedata.normalize("NFKC", text).translate(_QUOTES).split())


_EMOTION_CASE = {emotion.lower(): emotion for emotion in STORY_EMOTIONS}


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
        emotion = str(item.get("emotion", "")).strip()
        # "narrative" is the right answer in the wrong case, not a wrong answer
        emotion = _EMOTION_CASE.get(emotion.lower(), emotion)
        lines.append(StoryLine(text=item["text"].strip(), emotion=emotion))
    return lines


def _validate(source: str, lines: list[StoryLine]) -> None:
    """Raise StorySplitError if this split cannot be accepted."""
    for line in lines:
        problem = line_problem(line)
        if problem:
            raise StorySplitError(f"The split was rejected: {problem}")
    if _normalise(" ".join(line.text for line in lines)) != _normalise(source):
        raise StorySplitError("The split was rejected: it changed the story's wording")


def _too_long() -> StorySplitError:
    return StorySplitError(
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
        except StorySplitError as exc:
            last_error = exc
            logger.warning("Story split attempt %d rejected: %s", attempt, exc)
            continue
        logger.info("Story split into %d lines", len(lines))
        return lines

    assert last_error is not None
    raise last_error


# ── story fix ────────────────────────────────────────────────────────────────

# Hindi's is spelled out because Hindi is what the operator listens to; every
# other language gets the general rule.
_REGISTERS = {
    "hi": (
        "Everyday spoken Hindi, the way a parent tells a bedtime story. Keep the "
        "common English loanwords Hindi speakers actually use, such as स्कूल and बस, "
        "written in Devanagari; do not swap them for formal or Sanskritised Hindi words."
    ),
}


def _register(language: str) -> str:
    base = language.split("-")[0].lower()
    if base in _REGISTERS:
        return _REGISTERS[base]
    name = LANGUAGE_NAMES.get(language, language)
    return f"Everyday spoken {name}, the way a parent tells a story aloud; not formal or literary."


def _fix_instruction(language: str) -> str:
    name = LANGUAGE_NAMES.get(language, language)
    script = LANGUAGE_SCRIPT_HINTS.get(language, "native script")
    return f"""You rewrite literal machine translations of a children's story into natural {name} ({language}), as the story would be told aloud.

You are given the story's title and, for every line, its id, its English text, the emotion it is voiced with, and a draft translation.

Rules:
1) The audience is children, listening rather than reading. Every line is spoken aloud.
2) The drafts are literal machine translations. Reword freely so the story sounds told, not translated: keep each line's meaning, not the draft's wording.
3) Register: {_register(language)}
4) Keep names, pronouns, gender agreement and forms of address consistent across the whole story.
5) Keep each line's meaning inside that line. Each line is a separate audio file, so never move content from one line to another, and never merge or split lines.
6) Keep proper nouns (the names of people, animals and places) as they are.
7) Write in {script}. Fix script/orthography issues: redundant vowels, incorrect vowel signs/maatras, trailing halant/virama at word endings, malformed consonant clusters, and accidental repeated syllables.
8) Output valid JSON only (no markdown, no code fences, no commentary): one object with an entry for every line id, each value the fixed line as a plain string, e.g. {{"1": "...", "2": "..."}}"""


def _fix_contents(title: str, lines: dict[str, StoryLine], drafts: dict[str, str]) -> str:
    payload = [
        {"id": line_id, "english": line.text, "emotion": line.emotion, "draft": drafts[line_id]}
        for line_id, line in lines.items()
    ]
    return f"Title: {title}\nLines:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def _parse_fix(response_text: str, line_ids: list[str]) -> dict[str, str]:
    payload = _parse_response_json(response_text)
    if not isinstance(payload, dict):
        raise ValueError("the reply is not a JSON object")
    fixed: dict[str, str] = {}
    for line_id in line_ids:
        value = payload.get(line_id)
        if isinstance(value, str) and value.strip():
            fixed[line_id] = value.strip()
    return fixed


def fix_story(
    title: str,
    lines: dict[str, StoryLine],
    drafts: dict[str, str],
    language: str,
    *,
    settings: QCSettings,
) -> dict[str, str]:
    """Rewrite one language's drafts of a whole story so it sounds told.

    ``lines`` and ``drafts`` are keyed by line id, in story order; every line
    needs a draft. Returns fixed text keyed by line id. An id the reply left out
    or left empty is simply absent — the caller decides what that line gets.
    Raises StoryFixError if no model returns usable JSON.
    """
    client = genai.Client(api_key=settings.api_key)
    config = types.GenerateContentConfig(system_instruction=_fix_instruction(language))
    contents = _fix_contents(title, lines, drafts)
    line_ids = list(lines)

    last_exc: Exception | None = None
    for model in settings.models:
        try:
            response = retry_call(
                lambda: client.models.generate_content(model=model, contents=contents, config=config),
                operation=f"Gemini story fix {language} ({model})",
            )
            fixed = _parse_fix(response.text.strip(), line_ids)
        except Exception as exc:
            last_exc = exc
            logger.warning("Story fix %s: model %s failed: %s", language, model, exc)
            continue
        logger.info("Story fix %s: %d/%d lines fixed using %s", language, len(fixed), len(line_ids), model)
        return fixed
    raise StoryFixError(f"Gemini story fix for {language} failed after {len(settings.models)} models: {last_exc}")
