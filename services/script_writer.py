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
from services.script_parse import ScriptFacts, parse_script
from services.script_validate import (
    HISTORY_DAYS,
    MAX_NUMBER,
    MAX_TAGS,
    MIN_NUMBER,
    MIN_TAGS,
    TAG_BANK,
    HistoryFacts,
    Violation,
    hard,
    offending_keys,
    validate_drafts,
)

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

# Given in characters, not words: a Devanagari word is long enough that a word
# budget and the real duration come apart, and an earlier word-only instruction
# produced scripts half this length.
#
# Measured tag-stripped, unlike the raw limit above. The tags are instructions
# to the speech model and are never spoken, so counting them against a budget
# sized for spoken content would shrink the script every time the tag system
# grew — which it just did, from three tags to six.
#
# What the prompt ASKS for, and what the check ACCEPTS, are deliberately not the
# same numbers.
#
# Every spoken character is paid for twice — once to synthesise and once as
# video runtime — so the ask is set at the shortest length that still carries
# all six beats: hook, three predictions, the verbatim health line, the colour
# and number line, and the closing. The fixed furniture alone is ~155
# characters, which is why the ask does not go lower.
#
# The model writes to whatever ceiling it is given and overshoots it by roughly
# 5-10%: told 380-460 it produced ~479, told 450-550 it produced ~555. Asking
# for 350-420 therefore lands around 380-460, which is where the hand-authored
# reference sheet sat. Chasing the overshoot by widening acceptance is a
# treadmill — acceptance is set once, to what is genuinely tolerable, and the
# ask is what gets tuned.
ASK_CHARS_LOW = 350
ASK_CHARS_HIGH = 420

# What the check tolerates. Wider than the ask on both sides: a script a little
# over is not worth a rewrite, and a band nothing lands inside makes the
# soft-violation log noise — the warning-nobody-reads problem this work exists
# to remove. Both bounds are soft. The floor is a thinness guard: below it the
# three predictions have barely a clause each.
ACCEPT_CHARS_LOW = 300
ACCEPT_CHARS_HIGH = 550

# How many days of previous colours, numbers, areas and tags are listed back as
# "do not repeat these". Re-exported from the validator, which owns the number,
# so the window the prompt describes is the window the store actually read.
RECENT_DAYS_IN_PROMPT = HISTORY_DAYS

# How many times a set may be partially rewritten before the run is refused.
# Two is enough for the ordinary case — a colour clash the model fixes when
# told — without letting a badly-briefed category burn calls indefinitely.
MAX_REPAIR_ATTEMPTS = 2


class ScriptWriterError(Exception):
    pass


class ScriptRepairError(ScriptWriterError):
    """A set that still repeats after its repair budget.

    Distinct from the base error so model fallthrough can tell the two apart. A
    model that returns unusable JSON should be swapped for the next one; a model
    that returned twelve well-formed scripts which happen to collide has already
    been asked to fix them and failed, and running the whole budget again
    against the fallback would cost another handful of calls to land in the same
    place.
    """


@dataclass(frozen=True)
class DraftScript:
    """One generated video: what it is called, and what gets spoken.

    ``title`` becomes the NAS filename downstream, so it travels with the script
    from the moment it is written rather than being derived twice. ``key`` is
    the item key the model answered under, carried so history can be grouped per
    item without re-deriving the mapping; it defaults empty because callers that
    only read a draft have no use for it.
    """

    title: str
    script: str
    key: str = ""


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


_BEAT_LABELS = {
    "hook": "the opening hook",
    "prediction": "a prediction sentence",
    "transition": "a transition or point of emphasis",
    "health": "the health line",
    "fortune": "the colour and number line",
    "closing": "the closing encouragement",
}


def _tag_bank_block() -> str:
    """The permitted tags, rendered from the same dict the validator checks.

    Written out rather than summarised: a model given the closed list picks from
    it, and a model given a description of the list invents members of it.
    """
    missing = set(TAG_BANK) - set(_BEAT_LABELS)
    if missing:
        raise ScriptWriterError(f"tag bank beat(s) with no prompt label: {sorted(missing)}")
    return "\n".join(
        f"  {_BEAT_LABELS[beat]} — {', '.join(f'[{tag}]' for tag in tags)}"
        for beat, tags in TAG_BANK.items()
    )


def _prose_block(recent: list[DraftScript]) -> str:
    if not recent:
        return ""
    lines = "\n".join(f"- {d.title}: {d.script}" for d in recent)
    return f"""
Scripts you have already published on recent days. Do not reuse their phrasing,
their openings, their imagery or their predictions — a viewer who watched
yesterday must not recognise today's lines:
{lines}
"""


def _facts_block(history: dict[str, HistoryFacts], items: tuple[SetItem, ...]) -> str:
    """What each item has already used, as facts rather than prose.

    The prose excerpts above are truncated, and the colour and number sit at the
    end of a script — quoting excerpts alone meant the model was told to avoid
    colours it could not see. These are listed explicitly for that reason.
    """
    lines: list[str] = []
    for item in items:
        facts = history.get(item.key)
        if not facts:
            continue
        parts: list[str] = []
        if facts.colours:
            parts.append(f"colours used: {', '.join(facts.colours)}")
        if facts.numbers:
            parts.append(f"numbers used: {', '.join(str(n) for n in facts.numbers)}")
        if facts.combinations:
            combos = "; ".join(" + ".join(sorted(c)) for c in facts.combinations)
            parts.append(f"area combinations used: {combos}")
        if facts.previous_tags:
            # Listed because a soft rule checks for exactly this sequence
            # repeating. Left out, the model would be marked down for something
            # it was never shown.
            parts.append("yesterday's tags: " + " ".join(f"[{t}]" for t in facts.previous_tags))
        if parts:
            lines.append(f"- {item.key} ({item.title}) — " + " | ".join(parts))

    if not lines:
        return ""
    return f"""
Already used in the last {RECENT_DAYS_IN_PROMPT} days. Every one of these is
forbidden today for that item — a different shade of a listed colour still
counts as that colour:
{chr(10).join(lines)}
"""


def _system_instruction(*, brief: str, language: str, items: tuple[SetItem, ...]) -> str:
    language_name = LANGUAGE_NAMES.get(language, language)
    script_hint = LANGUAGE_SCRIPT_HINTS.get(language, "native script")
    item_lines = "\n".join(f'- "{item.key}" — refer to it as {item.title}' for item in items)
    tag_bank = _tag_bank_block()
    return f"""You write scripts to be SPOKEN to camera by a single presenter, in {language_name} ({script_hint}).

The brief for this series:
{brief}

Structure — every script is exactly three paragraphs, separated by a blank line:
a hook that names the subject and lands the theme, a body carrying the substance
of the brief in flowing sentences, and one closing line of direct encouragement.

DELIVERY TAGS. The scripts are voiced by ElevenLabs v3, which reads performance
direction only as English words in square brackets. A tag is never spoken aloud.
It applies to the phrase that immediately follows it, not to the paragraph.

Place one tag per beat — {MIN_TAGS}-{MAX_TAGS} for a script of the length asked
for below, proportionately fewer in a shorter one, and never more than
{MAX_TAGS}. Choose from this closed list and nothing else:
{tag_bank}

Tag rules, all absolute:
- English only. A Devanagari phrase in brackets is not a tag — the voice engine
  cannot read it. Never write [सामान्य गति], [शांत, आत्मविश्वासी आवाज़], [जोश से]
  or anything like them.
- One quality per bracket. No commas inside a tag, no compound descriptions.
- Never invent a tag to fill a position. A beat with no tag is correct; a
  placeholder tag is not.
- Vary which tag each beat takes from day to day and across items. Do not open
  every script with the same tag.

Rules:
1) Write for the ear, not the page. Natural spoken rhythm, no headings, no
   bullet points, no emoji, no stage directions outside the tags.
2) LENGTH IS A HARD REQUIREMENT: {ASK_CHARS_LOW}-{ASK_CHARS_HIGH}
   characters of spoken text per script, NOT counting the tags. Never let the
   whole string with tags exceed {MAX_SCRIPT_CHARS}.
3) Write entirely in {language_name}, in {script_hint}. Modern technical and
   business terms may stay as widely-used English loanwords written in
   {script_hint}. The delivery tags stay in English, as listed above.
4) Every script must stand alone and differ from the others in this set — vary
   the subject matter, the imagery and the sentence shapes across items. Only
   lines the brief explicitly asks to be repeated may be repeated.
5) Any lucky number the brief asks for is a whole number from {MIN_NUMBER} to
   {MAX_NUMBER}, never a single digit and never three digits.
6) Return valid JSON only: an object mapping each item key to its script string.
   No markdown, no code fences, no commentary.

The item keys, which you must use as the JSON keys exactly as written:
{item_lines}"""


def _repair_block(
    violations: list[Violation],
    items: tuple[SetItem, ...],
    *,
    in_use: dict[str, ScriptFacts],
) -> str:
    """What was wrong, named per item, for a rewrite request.

    Only the failures are rewritten. The rest of the set is not resent as text:
    it is already good, and re-offering it invites the model to change it.

    Their colours and numbers *are* sent, though. Without them a rewrite picks
    freely and lands on a colour some untouched script already holds — and
    because a same-day collision is blamed on the later of the pair, the next
    round then rejects a script that was clean to begin with. Left unfixed that
    walks down the zodiac and spends the whole budget on collisions the model
    was never warned about.
    """
    by_key: dict[str, list[str]] = {}
    for violation in violations:
        by_key.setdefault(violation.item_key, []).append(violation.detail)
    lines = "\n".join(
        f'- "{item.key}" ({item.title}): ' + "; ".join(by_key[item.key])
        for item in items if item.key in by_key
    )

    taken = {
        key: facts for key, facts in in_use.items() if key not in by_key
    }
    colours = sorted({f.colour for f in taken.values() if f.colour})
    numbers = sorted({f.number for f in taken.values() if f.number is not None})
    avoid = ""
    if colours or numbers:
        avoid = "\n\nAlready in use today by the scripts you are NOT rewriting — "
        avoid += "do not pick any of these either:"
        if colours:
            avoid += f"\n  colours: {', '.join(colours)}"
        if numbers:
            avoid += f"\n  numbers: {', '.join(str(n) for n in numbers)}"

    return f"""These scripts were rejected. Rewrite each one completely, fixing
exactly what is listed and keeping everything else the brief asks for:
{lines}{avoid}

Return valid JSON only, containing exactly these keys and no others."""


def write_daily_scripts(
    *,
    brief: str,
    language: str,
    publish_date: str,
    items: tuple[SetItem, ...] = ZODIAC_SIGNS,
    recent: list[DraftScript] | None = None,
    history: dict[str, HistoryFacts] | None = None,
    settings: ScriptWriterSettings,
) -> list[DraftScript]:
    """Write one script per item for a given day, and refuse a set that repeats.

    One request for the whole set, not one per item: the model can then vary the
    twelve against each other, and a run costs a single call. Falls through the
    configured models in order, as QC does.

    What comes back is validated rather than trusted. A set with hard violations
    — an illegal delivery tag, a colour or number that repeats within the day or
    across the history window — is partially rewritten: only the offending items
    are re-requested, up to ``MAX_REPAIR_ATTEMPTS``, and the rest are kept byte
    for byte. Rewriting the whole set would perturb scripts that were fine and
    cost a full call each time.

    A repair call cannot vary the twelve against each other the way the first
    call does, but it does not need to: the same-day collision rules are
    evaluated against the full set including the untouched scripts, so the
    cross-check the single call made implicitly is made explicitly here.

    :param brief: The category's script brief — what this series is about.
    :param language: A language code from services/languages.py.
    :param publish_date: ISO date the scripts are for. Reaches the prompt, so
        "today" means the day the operator is filing under, not the model's idea
        of today.
    :param items: The set to write — each carries the key the model answers
        under and the title the file is named with.
    :param recent: Previously generated scripts, quoted as prose to write away
        from.
    :param history: Per item key, the colours, numbers and area combinations
        already used over the history window. Absent means no history, which is
        a first run rather than an error.
    :param settings: Resolved configuration, from the edge of the request.
    :raises ScriptWriterError: If no model produced a usable set, or if hard
        violations survived the repair budget.
    """
    if not brief.strip():
        raise ScriptWriterError("script brief must not be empty")
    if not items:
        raise ScriptWriterError("no items to write scripts for")

    history = history or {}
    client = genai.Client(api_key=settings.api_key)
    config = types.GenerateContentConfig(
        system_instruction=_system_instruction(brief=brief, language=language, items=items),
    )
    # The length and shape rules are repeated here, in the last thing the model
    # reads. Stated only in the system instruction, length came back at roughly
    # half what was asked for, every time.
    prompt = f"""Write today's scripts. Today is {publish_date}.
{_facts_block(history, items)}{_prose_block(recent or [])}
Before you answer, check each script: three paragraphs, one English delivery tag
per beat with no Devanagari inside any bracket, and
{ASK_CHARS_LOW}-{ASK_CHARS_HIGH} characters of spoken text not counting
the tags. Fix any that miss this. Do not pad with repetition."""

    last_exc: Exception | None = None
    for model in settings.models:
        try:
            drafts = _write_with_model(
                client=client,
                model=model,
                config=config,
                prompt=prompt,
                items=items,
                history=history,
            )
            logger.info("Wrote %d script(s) for %s using %s", len(drafts), publish_date, model)
            return drafts
        except ScriptRepairError:
            # A collision the model would not fix is not something the fallback
            # model gets another full budget to reproduce.
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning("Script writer: model %s failed: %s", model, exc)

    raise ScriptWriterError(
        f"Gemini script writing failed after {len(settings.models)} model(s): {last_exc}"
    ) from last_exc


def _write_with_model(
    *,
    client: genai.Client,
    model: str,
    config: types.GenerateContentConfig,
    prompt: str,
    items: tuple[SetItem, ...],
    history: dict[str, HistoryFacts],
) -> list[DraftScript]:
    """One model's attempt at the set, including its repair rounds.

    Model fallthrough sits outside this, so a model that cannot return parseable
    JSON is swapped out before any repair is attempted — there is no point
    asking a broken response to fix itself.
    """
    def generate(contents: str, wanted: tuple[SetItem, ...]) -> dict[str, str]:
        response = retry_call(
            lambda: client.models.generate_content(
                model=model, contents=contents, config=config
            ),
            operation=f"Gemini script writer ({model})",
        )
        return _collect(_parse_response_json((response.text or "").strip()), wanted)

    scripts = generate(prompt, items)
    truncated = _truncate_set(scripts)

    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        facts = {key: parse_script(text) for key, text in scripts.items()}
        violations = validate_drafts(
            facts,
            history=history,
            truncated=truncated,
            target_low=ACCEPT_CHARS_LOW,
            target_high=ACCEPT_CHARS_HIGH,
            raw=scripts,
        )
        blocking = hard(violations)
        for violation in violations:
            if not violation.hard:
                logger.info("Script writer: soft violation — %s", violation)

        if not blocking:
            break
        if attempt == MAX_REPAIR_ATTEMPTS:
            raise ScriptRepairError(
                "scripts still repeat or break the tag rules after "
                f"{MAX_REPAIR_ATTEMPTS} repair attempt(s): "
                + "; ".join(str(v) for v in blocking)
            )

        failing = offending_keys(blocking)
        logger.warning(
            "Script writer: repairing %d of %d script(s) — %s",
            len(failing), len(scripts), ", ".join(failing),
        )
        subset = tuple(item for item in items if item.key in failing)
        rewritten = generate(
            prompt + "\n\n" + _repair_block(blocking, subset, in_use=facts), subset
        )
        # Trim before copying across: _truncate_set mutates the dict it is
        # given, so updating `scripts` first would leave the untrimmed text in
        # the dict that actually ships.
        truncated = (truncated - frozenset(failing)) | _truncate_set(rewritten)
        scripts.update(rewritten)

    return [
        DraftScript(title=item.title, script=scripts[item.key], key=item.key)
        for item in items
    ]


def _truncate_set(scripts: dict[str, str]) -> frozenset[str]:
    """Which scripts had to be trimmed, and trim them in place.

    A trimmed script has lost its closing line, so this is reported as a
    violation rather than logged and forgotten. The trim still happens: if the
    repair budget runs out on some other item, what ships must at least fit.
    """
    over = frozenset(key for key, text in scripts.items() if len(text.strip()) > MAX_SCRIPT_CHARS)
    for key in over:
        scripts[key] = fit_to_limit(scripts[key])
    return over


def _collect(written: object, items: tuple[SetItem, ...]) -> dict[str, str]:
    """Turn a parsed response into scripts by item key, or refuse it.

    A missing item is fatal for the whole attempt rather than skipped: the next
    model gets a chance, and the operator gets twelve signs or an error — never
    a set that is quietly short one.

    Keyed rather than titled because validation and repair both address items by
    the key the model answers under; titles are put back on at the very end.
    Length is not enforced here — an overlong script is trimmed and reported by
    the caller, so the trim can be a violation rather than a silent repair.
    """
    if not isinstance(written, dict):
        raise ScriptWriterError("model returned JSON that is not an object of item -> script")

    scripts: dict[str, str] = {}
    for item in items:
        value = written.get(item.key)
        if not isinstance(value, str) or not value.strip():
            raise ScriptWriterError(f"model returned no script for {item.key}")
        scripts[item.key] = value.strip()
    return scripts
