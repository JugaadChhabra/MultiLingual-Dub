"""Writing a day's scripts, so nobody has to type them into a spreadsheet.

The video pipeline used to take its words from an Excel sheet: one row per
video, authored by hand every day. This module authors them instead. Gemini is
the writer — there is no external content source and nothing is fetched. What it
is told is the category's brief, the date, the item list (for a horoscope, the
twelve signs) and what it already said on recent days.

Output is a *draft*. Nothing here submits a render: an operator reads and edits
these before a single paid render is spent, which is the whole reason generation
and rendering are separate steps.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import google.genai as genai
from google.genai import types

from services.languages import LANGUAGE_NAMES, LANGUAGE_SCRIPT_HINTS
from services.retry import retry_call
from services.runtime_config import RuntimeConfig, read_setting, require

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]

@dataclass(frozen=True)
class SetItem:
    """One item in a set: the key the model answers under, and the file name.

    Two fields rather than one because the reference sheet the editor has been
    filling by hand files each sign under its Devanagari name, while a model
    keys JSON far more reliably off an unambiguous Latin name. The title is what
    reaches the NAS; the key never leaves this module's prompt.
    """

    key: str
    title: str


# The zodiac, in the order it is always read, titled the way the hand-authored
# sheet titles it. Fixed rather than configurable: the set is a property of what
# a horoscope IS, and a run that quietly produced eleven signs would be a bug
# nobody notices until a sign is missing from the NAS folder.
ZODIAC_SIGNS = (
    SetItem("Aries", "मेष"), SetItem("Taurus", "वृषभ"), SetItem("Gemini", "मिथुन"),
    SetItem("Cancer", "कर्क"), SetItem("Leo", "सिंह"), SetItem("Virgo", "कन्या"),
    SetItem("Libra", "तुला"), SetItem("Scorpio", "वृश्चिक"), SetItem("Sagittarius", "धनु"),
    SetItem("Capricorn", "मकर"), SetItem("Aquarius", "कुंभ"), SetItem("Pisces", "मीन"),
)

# The single-script textarea and HeyGen both stop at 1000 characters, so a draft
# longer than this could not be rendered as written. The prompt asks for a length
# well inside it; this is the guard for when the model overruns anyway.
MAX_SCRIPT_CHARS = 1000

# Measured off the hand-authored reference sheet, whose twelve scripts run
# 381-441 characters including their audio tags. Given in characters, not words:
# a Devanagari word is long enough that a word budget and the real duration come
# apart, and an earlier word-only instruction produced scripts half this length.
TARGET_CHARS_LOW = 380
TARGET_CHARS_HIGH = 460

# How many days of previous scripts are quoted back as "do not repeat these".
# Enough to kill the obvious sameness; small enough that the prompt stays short.
RECENT_DAYS_IN_PROMPT = 7


class ScriptWriterError(Exception):
    pass


@dataclass(frozen=True)
class DraftScript:
    """One generated video: what it is called, and what gets spoken.

    ``title`` becomes the NAS filename downstream, so it travels with the script
    from the moment it is written rather than being derived twice.
    """

    title: str
    script: str


@dataclass(frozen=True)
class ScriptWriterSettings:
    api_key: str
    models: list[str]

    REQUIRED = ("GEMINI_API_KEY",)

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> ScriptWriterSettings:
        values = require(cls.REQUIRED, session)
        raw = read_setting("GEMINI_SCRIPT_MODELS", session).strip()
        models = [item.strip() for item in raw.split(",") if item.strip()] or DEFAULT_SCRIPT_MODELS
        return cls(api_key=values["GEMINI_API_KEY"], models=models)


def _parse_response_json(response_text: str) -> dict:
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[7:-3]
    elif text.startswith("```"):
        text = text[3:-3]
    return json.loads(text)


def fit_to_limit(script: str, *, limit: int = MAX_SCRIPT_CHARS) -> str:
    """Trim an overlong script at a sentence boundary.

    A hard slice mid-word would be spoken as a hard slice mid-word. Danda and
    full stop are both looked for because the scripts are as often Devanagari as
    Latin. If neither is found in range the text is cut at the last space.
    """
    script = script.strip()
    if len(script) <= limit:
        return script

    head = script[:limit]
    cut = max(head.rfind("।"), head.rfind("."), head.rfind("!"), head.rfind("?"))
    if cut < limit // 2:
        cut = head.rfind(" ")
    trimmed = (head[: cut + 1] if cut > 0 else head).strip()
    logger.warning("Script overran %d chars, trimmed to %d", len(script), len(trimmed))
    return trimmed


def _avoid_block(recent: list[DraftScript]) -> str:
    if not recent:
        return ""
    lines = "\n".join(f"- {d.title}: {d.script}" for d in recent)
    return f"""
Scripts you have already published on recent days. Do not reuse their phrasing,
their openings, their imagery or their predictions — a viewer who watched
yesterday must not recognise today's lines:
{lines}
"""


def _system_instruction(*, brief: str, language: str, items: tuple[SetItem, ...]) -> str:
    language_name = LANGUAGE_NAMES.get(language, language)
    script_hint = LANGUAGE_SCRIPT_HINTS.get(language, "native script")
    item_lines = "\n".join(f'- "{item.key}" — refer to it as {item.title}' for item in items)
    return f"""You write scripts to be SPOKEN to camera by a single presenter, in {language_name} ({script_hint}).

The brief for this series:
{brief}

Structure — every script is exactly three paragraphs, separated by a blank line,
and each paragraph BEGINS with an audio tag in square brackets:

  [<voice direction>] <the hook: one line that names the subject and lands the theme>

  [सामान्य गति] <the body: the substance of the brief, in flowing sentences>

  [<emotion>] <one closing line of direct encouragement>

The tags are performance directions for the speech model — they are never spoken
aloud, so they must be in square brackets and must not be worked into the
sentences. Use [शांत, आत्मविश्वासी आवाज़] for the first paragraph and
[सामान्य गति] for the second. For the third, choose a tag that fits what you
wrote — for example [जोश से], [गर्व से], [प्रेम से], [शांति से].

Rules:
1) Write for the ear, not the page. Natural spoken rhythm, no headings, no
   bullet points, no emoji, no stage directions outside the three tags.
2) LENGTH IS A HARD REQUIREMENT: {TARGET_CHARS_LOW}-{TARGET_CHARS_HIGH}
   characters per script, tags included. Never exceed {MAX_SCRIPT_CHARS}.
3) Write entirely in {language_name}, in {script_hint}. Modern technical and
   business terms may stay as widely-used English loanwords written in
   {script_hint}. The audio tags stay in Devanagari as given above.
4) Every script must stand alone and differ from the others in this set — vary
   the subject matter, the imagery and the sentence shapes across items. Only
   lines the brief explicitly asks to be repeated may be repeated.
5) Return valid JSON only: an object mapping each item key to its script string.
   No markdown, no code fences, no commentary.

The item keys, which you must use as the JSON keys exactly as written:
{item_lines}"""


def write_daily_scripts(
    *,
    brief: str,
    language: str,
    publish_date: str,
    items: tuple[SetItem, ...] = ZODIAC_SIGNS,
    recent: list[DraftScript] | None = None,
    settings: ScriptWriterSettings,
) -> list[DraftScript]:
    """Write one script per item for a given day.

    One request for the whole set, not one per item: the model can then vary the
    twelve against each other, and a run costs a single call. Falls through the
    configured models in order, as QC does.

    :param brief: The category's script brief — what this series is about.
    :param language: A language code from services/languages.py.
    :param publish_date: ISO date the scripts are for. Reaches the prompt, so
        "today" means the day the operator is filing under, not the model's idea
        of today.
    :param items: The set to write — each carries the key the model answers
        under and the title the file is named with.
    :param recent: Previously generated scripts to write away from.
    :param settings: Resolved configuration, from the edge of the request.
    """
    if not brief.strip():
        raise ScriptWriterError("script brief must not be empty")
    if not items:
        raise ScriptWriterError("no items to write scripts for")

    client = genai.Client(api_key=settings.api_key)
    config = types.GenerateContentConfig(
        system_instruction=_system_instruction(brief=brief, language=language, items=items),
    )
    # The length and shape rules are repeated here, in the last thing the model
    # reads. Stated only in the system instruction, length came back at roughly
    # half what was asked for, every time.
    prompt = f"""Write today's scripts. Today is {publish_date}.
{_avoid_block(recent or [])}
Before you answer, check each script: three paragraphs, each opening with its
square-bracket audio tag, and {TARGET_CHARS_LOW}-{TARGET_CHARS_HIGH} characters
in total. Fix any that miss this. Do not pad with repetition."""

    last_exc: Exception | None = None
    for model in settings.models:
        try:
            response = retry_call(
                lambda: client.models.generate_content(
                    model=model, contents=prompt, config=config
                ),
                operation=f"Gemini script writer ({model})",
            )
            written = _parse_response_json((response.text or "").strip())
            drafts = _collect(written, items)
            logger.info("Wrote %d script(s) for %s using %s", len(drafts), publish_date, model)
            return drafts
        except Exception as exc:
            last_exc = exc
            logger.warning("Script writer: model %s failed: %s", model, exc)

    raise ScriptWriterError(
        f"Gemini script writing failed after {len(settings.models)} model(s): {last_exc}"
    ) from last_exc


def _collect(written: object, items: tuple[SetItem, ...]) -> list[DraftScript]:
    """Turn a parsed response into drafts, or refuse it.

    A missing item is fatal for the whole attempt rather than skipped: the next
    model gets a chance, and the operator gets twelve signs or an error — never
    a set that is quietly short one.
    """
    if not isinstance(written, dict):
        raise ScriptWriterError("model returned JSON that is not an object of item -> script")

    drafts: list[DraftScript] = []
    for item in items:
        value = written.get(item.key)
        if not isinstance(value, str) or not value.strip():
            raise ScriptWriterError(f"model returned no script for {item.key}")
        drafts.append(DraftScript(title=item.title, script=fit_to_limit(value)))
    return drafts
