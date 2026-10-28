"""Writing a day's scripts, so nobody has to type them into a spreadsheet.

The video pipeline used to take its words from an Excel sheet: one row per
video, authored by hand every day. This module authors them instead. A model
reached through OpenRouter is the writer — there is no external content source and nothing is fetched. What it
is told is the category's brief, the date, the item list (for a horoscope, the
twelve signs) and what it already said on recent days.

Output is a *draft*. Nothing here submits a render: an operator reads and edits
these before a single paid render is spent, which is the whole reason generation
and rendering are separate steps.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from services.errors import TOP_UP_URLS, classify
from services.languages import LANGUAGE_NAMES, LANGUAGE_SCRIPT_HINTS
from services.openrouter import OpenRouterClient
from services.retry import retry_call
from services.runtime_config import RuntimeConfig, read_setting, require
from services.script_history import DraftScript, ScriptHistoryStore
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

# OpenRouter model ids. Flash writes the set for a fraction of a cent; Pro is
# the fallback for a day Flash cannot get right.
DEFAULT_SCRIPT_MODELS = ["google/gemini-2.5-flash", "google/gemini-2.5-pro"]

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


class ScriptRequestError(ScriptWriterError):
    """A request the writer refuses before any model is called.

    Separate from a generation failure because the fix is the operator's — add
    a brief, pick a real item set — not a retry against the model.
    """


class UnknownItemTitleError(ScriptRequestError):
    """Submitted titles that cannot be mapped back to the item they were written as.

    Raised for a batch that looks like a horoscope — some title is a sign name —
    but carries titles that are not. Filed anyway, those rows would land under a
    key nothing matches and drop out of the uniqueness window without a trace.
    """

    def __init__(self, titles: list[str]) -> None:
        self.titles = titles
        super().__init__(
            "This looks like a horoscope batch, but these titles are not "
            f"zodiac sign names: {', '.join(titles)}. Their scripts would "
            "vanish from the uniqueness history. Fix the titles, or submit "
            "them as a non-horoscope batch."
        )


class ScriptRepairError(ScriptWriterError):
    """A set that still repeats after its repair budget.

    Distinct from the base error so model fallthrough can tell the two apart. A
    model that returns unusable JSON should be swapped for the next one; a model
    that returned twelve well-formed scripts which happen to collide has already
    been asked to fix them and failed, and running the whole budget again
    against the fallback would cost another handful of calls to land in the same
    place.

    It carries the best-effort set anyway. The scripts are all present and
    well-formed — what remains is a collision or a tag the model would not fix —
    so the caller can hand the operator all twelve to review and fix the one
    that clashed, which is far better than an error and a full re-run. The
    unresolved hard violations ride along so the review can point at them.
    """

    def __init__(
        self,
        message: str,
        *,
        drafts: list[DraftScript] | None = None,
        violations: list[Violation] | None = None,
    ) -> None:
        super().__init__(message)
        self.drafts = drafts or []
        self.violations = violations or []


class ScriptCreditsError(ScriptWriterError):
    """The OpenRouter account behind the key has no credit left.

    Every configured model bills to the same balance, so the fallback would only
    hit the same wall — and bury the one line that says what to do under a
    warning per model. The fix is a top-up, not a retry.
    """


def _is_out_of_credit(exc: Exception) -> bool:
    """A billing failure, as opposed to one model's own rate limit.

    A 429 can be worded as quota too, but it is one model's provider being
    busy or limited — the next model may well have room — so a 429 is never a
    stop.
    """
    if getattr(exc, "status_code", None) == 429:
        return False
    return classify(exc, provider="openrouter").kind == "quota"


@dataclass(frozen=True)
class ScriptWriterSettings:
    api_key: str
    models: list[str]

    REQUIRED = ("OPENROUTER_API_KEY",)

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> ScriptWriterSettings:
        values = require(cls.REQUIRED, session)
        raw = read_setting("OPENROUTER_SCRIPT_MODELS", session).strip()
        models = [item.strip() for item in raw.split(",") if item.strip()] or DEFAULT_SCRIPT_MODELS
        return cls(api_key=values["OPENROUTER_API_KEY"], models=models)


def _parse_response_json(response_text: str) -> dict:
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[7:-3]
    elif text.startswith("```"):
        text = text[3:-3]
    return json.loads(text)


def _response_schema(items: tuple[SetItem, ...]) -> dict:
    """A JSON schema forcing an object with exactly the item keys, each a string.

    Handed to the model as a strict response schema so the
    reply comes back as a JSON object of the right shape with every key present,
    rather than free text that is parsed and then found wanting. This removes a
    whole class of first-call failures — a stray code fence, a missing sign, a
    reply that is a list or a preamble — that used to burn a model fallthrough.

    Built per call from the items actually requested: a repair asks for only the
    offending subset, and a schema demanding the whole set would reject the very
    reply it asked for.
    """
    keys = [item.key for item in items]
    return {
        "type": "object",
        "properties": {key: {"type": "string"} for key in keys},
        "required": keys,
        "additionalProperties": False,
    }


def _recent_by_key(recent: list[DraftScript]) -> dict[str, list[str]]:
    """Prior days' script text grouped by item key, for the across-day check.

    Records carry the key the model answered under; ones without it — older
    history written before keys were stored — are dropped rather than guessed
    at, the same way the facts window drops them.
    """
    out: dict[str, list[str]] = {}
    for draft in recent:
        if draft.key:
            out.setdefault(draft.key, []).append(draft.script)
    return out


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
   {MAX_NUMBER} — never zero, never negative, never three digits.
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
    recent_by_key = _recent_by_key(recent or [])
    client = OpenRouterClient(settings.api_key)
    system_instruction = _system_instruction(brief=brief, language=language, items=items)
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
                system_instruction=system_instruction,
                prompt=prompt,
                items=items,
                history=history,
                recent_by_key=recent_by_key,
            )
            logger.info("Wrote %d script(s) for %s using %s", len(drafts), publish_date, model)
            return drafts
        except ScriptRepairError:
            # A collision the model would not fix is not something the fallback
            # model gets another full budget to reproduce.
            raise
        except Exception as exc:
            if _is_out_of_credit(exc):
                logger.error("Script writer: OpenRouter credits exhausted (%s): %s", model, exc)
                raise ScriptCreditsError(
                    "OpenRouter credits are used up, so no model can write scripts. "
                    f"Add credits for the account behind OPENROUTER_API_KEY at "
                    f"{TOP_UP_URLS['openrouter']}, then try again."
                ) from exc
            last_exc = exc
            logger.warning("Script writer: model %s failed: %s", model, exc)

    raise ScriptWriterError(
        f"Script writing failed after {len(settings.models)} model(s): {last_exc}"
    ) from last_exc


def _write_with_model(
    *,
    client: OpenRouterClient,
    model: str,
    system_instruction: str,
    prompt: str,
    items: tuple[SetItem, ...],
    history: dict[str, HistoryFacts],
    recent_by_key: dict[str, list[str]] | None = None,
) -> list[DraftScript]:
    """One model's attempt at the set, including its repair rounds.

    Model fallthrough sits outside this, so a model that cannot return parseable
    JSON is swapped out before any repair is attempted — there is no point
    asking a broken response to fix itself.
    """
    def generate(contents: str, wanted: tuple[SetItem, ...]) -> dict[str, str]:
        # Config is built per call: the response schema names exactly the keys
        # being asked for, which differs between the first call and a repair.
        schema = _response_schema(wanted)
        text = retry_call(
            lambda: client.complete_json(
                model=model, system=system_instruction, prompt=contents, schema=schema
            ),
            operation=f"Script writer ({model})",
        )
        return _collect(_parse_response_json(text.strip()), wanted)

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
            recent_by_key=recent_by_key,
        )
        blocking = hard(violations)
        for violation in violations:
            if not violation.hard:
                logger.info("Script writer: soft violation — %s", violation)

        if not blocking:
            break
        if attempt == MAX_REPAIR_ATTEMPTS:
            # Every script is present and well-formed — what survived is a
            # collision or a tag the model would not fix. Hand the whole set
            # back on the error so the operator can review twelve and fix the
            # one that clashed, rather than getting nothing and re-running.
            partial = [
                DraftScript(title=item.title, script=scripts[item.key], key=item.key)
                for item in items
            ]
            raise ScriptRepairError(
                "scripts still repeat or break the tag rules after "
                f"{MAX_REPAIR_ATTEMPTS} repair attempt(s): "
                + "; ".join(str(v) for v in blocking),
                drafts=partial,
                violations=blocking,
            )

        failing = offending_keys(blocking)
        logger.warning(
            "Script writer: repairing %d of %d script(s) — %s",
            len(failing), len(scripts), ", ".join(failing),
        )
        # The reasons, not just the keys: a repair round that never resolves is
        # otherwise a count with no cause, and the cause is what the operator
        # needs to see. Only the surviving-budget failure logged these before.
        for violation in blocking:
            logger.warning("Script writer: hard violation — %s", violation)
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


@dataclass(frozen=True)
class DaySet:
    """A day's drafts, and what is still wrong with them.

    ``warnings`` is empty for a clean set. It is non-empty only when the repair
    budget ran out: every draft is present, and the warnings name the clashes
    the operator has to fix by hand before running.
    """

    drafts: list[DraftScript]
    warnings: list[str] = field(default_factory=list)


_MP4_SUFFIX = re.compile(r"\.mp4$", re.IGNORECASE)


def _one_off_name(raw: str | None) -> str:
    """The name a one-off is keyed and filed under: spaces as underscores, a
    trailing .mp4 dropped as intake drops it. Only that suffix goes, so a dot
    inside a title ("Dr. Sharma tips", "v1.2 launch") is kept, not read as an
    extension that truncates the key."""
    return _MP4_SUFFIX.sub("", (raw or "").strip()).replace(" ", "_") or "Script"


def _one_off(name: str) -> SetItem:
    """A one-off video's item: its file name is both its key and its title.

    The one rule both directions share — writing a one-off day, and mapping a
    submitted one-off row back to its key. Callers pass a name
    already run through :func:`_one_off_name`.
    """
    return SetItem(key=name, title=name)


def items_for(item_set: str, title: str | None) -> tuple[SetItem, ...]:
    """The set a day is written for: the twelve signs, or a one-off of one.

    A one-off video is its own set of one — the operator's file name is both the
    key the model answers under and the name the render is filed as.
    """
    if item_set == "zodiac":
        return ZODIAC_SIGNS
    if item_set == "single":
        return (_one_off(_one_off_name(title)),)
    raise ScriptRequestError("item_set must be 'zodiac' or 'single'")


_SIGN_BY_TITLE = {item.title: item for item in ZODIAC_SIGNS}


def items_for_titles(titles: list[str]) -> list[SetItem]:
    """The items a batch's rows were written as, one per title.

    A submitted row carries only its title, because that is what names the file.
    History is grouped by item key, so the title is mapped back through the same
    sets a day is written from: a sign's Devanagari title to its Latin key, and
    a one-off title to itself.

    A batch with any sign title is a horoscope, and then *every* title must be a
    sign — see :class:`UnknownItemTitleError`. A batch with none is one-offs.

    :raises UnknownItemTitleError: For a horoscope batch with a title that is not a sign.
    """
    if not any(title in _SIGN_BY_TITLE for title in titles):
        return [_one_off(_one_off_name(title)) for title in titles]
    unmapped = sorted({t for t in titles if t not in _SIGN_BY_TITLE})
    if unmapped:
        raise UnknownItemTitleError(unmapped)
    return [_SIGN_BY_TITLE[title] for title in titles]


class ScriptWriter:
    """Writes a day for a category, and keeps Script history while doing it.

    The one place generation and history meet. A day is read from history in
    the two shapes the writer needs — prose excerpts for phrasing, per-item facts
    for the colour, number, area and tag rules — written, then recorded under
    its day, category and language, replacing any earlier attempt at that day.

    A set whose repair budget ran out is still recorded and returned: the model
    said it, so tomorrow has to avoid it, and the operator is better served by
    twelve drafts and a warning than by nothing.

    Settings arrive per call, not at construction, because they are resolved
    from the requesting operator's session rather than once per process.
    ``write`` is the generation step; it is a parameter so the HTTP layer can be
    tested against real history without a model behind it.

    A day that gets rendered is recorded twice: once when written, and again by
    :meth:`record_submitted` from the rows the operator actually submitted.
    """

    def __init__(self, history: ScriptHistoryStore, *, write=None) -> None:
        self._history = history
        self._write = write or write_daily_scripts

    def write_day(
        self,
        *,
        category: str,
        language: str,
        publish_date: str,
        brief: str,
        item_set: str,
        title: str | None = None,
        settings: ScriptWriterSettings,
    ) -> DaySet:
        """Write, record and return one day's set for a category.

        ``item_set`` is ``"zodiac"`` or ``"single"``; it arrives unchecked from
        the request, and anything else is refused here.

        :raises ScriptRequestError: For an empty brief or an unknown item set.
        :raises ScriptWriterError: If no model produced a usable set.
        """
        if not brief.strip():
            raise ScriptRequestError(
                "This category has no script brief. Add one in the category editor."
            )
        items = items_for(item_set, title)

        # ``before`` keeps the day being written out of its own history, so a
        # regeneration is not told to avoid the draft it is replacing.
        recent = self._history.recent(category=category, language=language, before=publish_date)
        facts = self._history.facts(category=category, language=language, before=publish_date)

        warnings: list[str] = []
        try:
            drafts = self._write(
                brief=brief,
                language=language,
                publish_date=publish_date,
                items=items,
                recent=recent,
                history=facts,
                settings=settings,
            )
        except ScriptRepairError as exc:
            if not exc.drafts:
                raise
            drafts = exc.drafts
            warnings = [str(v) for v in exc.violations]

        self._history.record(
            publish_date=publish_date, category=category, language=language, drafts=drafts
        )
        return DaySet(drafts=drafts, warnings=warnings)

    def record_submitted(
        self,
        *,
        category: str | None,
        language: str | None,
        publish_date: str | None,
        rows: list[tuple[str, str]],
    ) -> bool:
        """Record the rows an operator submitted, replacing the day's history.

        This is the write that matters for tomorrow: the operator's edits are
        what a viewer hears, so they are what later days must avoid, not the
        drafts recorded by :meth:`write_day`.

        ``rows`` are ``(title, script)`` pairs. Rows arriving without a
        category, language and date — the fields that identify a written day —
        cannot be filed and are not recorded; that is logged rather than
        refused, because recording is a side effect of a run, never a
        precondition for one. Logged loudly, because the cost is real: future
        days will be told to avoid the generated drafts, not the aired versions.

        :returns: Whether the rows were recorded.
        :raises UnknownItemTitleError: For a horoscope batch with a non-sign title.
        """
        if not (category and language and publish_date):
            logger.warning(
                "Submitted rows not recorded to script history: category/language/"
                "publish_date are incomplete (category=%r language=%r "
                "publish_date=%r), so future days will avoid the drafts, not the "
                "aired versions.",
                category, language, publish_date,
            )
            return False

        items = items_for_titles([title for title, _ in rows])
        self._history.record(
            publish_date=publish_date,
            category=category,
            language=language,
            drafts=[
                DraftScript(title=title, script=script, key=item.key)
                for (title, script), item in zip(rows, items)
            ],
        )
        return True
