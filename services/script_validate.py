"""Checking a day's scripts before they are kept.

The prompt asks for variety; this decides whether it got any. Two kinds of
sameness matter and they are checked differently: what repeats *within* a day
across the twelve signs, and what repeats *across* days for one sign. Both need
the whole set at once, which is why this runs on the set rather than per script.

Severity is the load-bearing idea. A hard violation means the script is wrong
and must be rewritten — an illegal tag reaches the speech model, a colour
collision is visible to anyone who watches two signs. A soft violation means it
is merely duller than intended, and a duller script still ships. Making
everything hard would let one borderline colour match fail a whole day's run.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re

from services.script_parse import (
    ScriptFacts,
    colours_collide,
    has_devanagari,
    strip_tags,
)
from services.text_similarity import similarity

logger = logging.getLogger(__name__)

# The delivery tags ElevenLabs v3 acts on. English because that is what the
# model interprets — a Devanagari mood word in brackets is decorative to a
# reader and invisible to the voice engine. One quality per bracket: a
# two-quality tag like "[calm, confident]" is not a tag v3 recognises.
#
# This dict is the single source: the prompt is rendered from it and the check
# below reads it, so the two cannot drift apart.
TAG_BANK: dict[str, tuple[str, ...]] = {
    "hook": ("warm", "authoritative", "confident"),
    "prediction": ("reassuring", "optimistic", "measured", "encouraging", "thoughtful", "hopeful"),
    "transition": ("pause", "slight emphasis", "softly"),
    "health": ("calm", "steady", "gentle"),
    "fortune": ("bright", "playful"),
    "closing": ("uplifting", "warm", "sincere", "joyful"),
}

ALL_TAGS: frozenset[str] = frozenset(tag for tags in TAG_BANK.values() for tag in tags)

# One per beat, with room for a transition or two. Fewer reads as untagged
# prose; more is a tag per sentence, which flattens the delivery it is meant to
# shape. These are the counts for a script at the horoscope's length, which is
# what the prompt asks for. The ceiling sits at nine because the model reliably
# places a tag on every beat of a full horoscope — nine on a ~380-char script —
# and rejecting that made the harness, not the copy, the reason a run stalled.
MIN_TAGS = 5
MAX_TAGS = 9

# Tags belong to beats, and a shorter script has fewer beats. A one-off promo is
# not a horoscope: demanding five tags of a two-sentence script would fail every
# generation for the operator's own categories, which do not share the
# horoscope's length. So the permitted count is derived from the spoken length
# rather than fixed, at roughly one tag per this many characters.
#
# The prompt still asks for MIN_TAGS-MAX_TAGS, because that is right for the
# length it also asks for. The check is looser on purpose: its job is to reject
# what is broken, not what is merely less tagged than ideal.
CHARS_PER_TAG = 50
FLOOR_TAGS = 2

# One or two digits (1-99). Single digits were once excluded as reading like an
# afterthought, but the brief now asks for them; a triple digit is still not a
# lucky number, and zero is not one either. The range gives 99 values to spread
# twelve signs across without contrivance.
MIN_NUMBER = 1
MAX_NUMBER = 99

# How far back uniqueness reaches. Defined here, where both the store that reads
# the window and the prompt that describes it can see one number: stated twice,
# they drift, and the prompt then tells the model to avoid ten days of colours
# while being handed seven.
HISTORY_DAYS = 10

# How many days of scripts are quoted as prose, for phrasing rather than facts.
# Far shorter, because excerpts are what dominate a prompt.
PROSE_DAYS = 3


@dataclass(frozen=True)
class Violation:
    """One thing wrong with one script.

    ``item_key`` is the key the model answers under, so a repair request can
    name exactly which scripts to rewrite without re-deriving the mapping.
    """

    item_key: str
    rule: str
    detail: str
    hard: bool

    def __str__(self) -> str:
        return f"{self.item_key}: {self.detail}"


@dataclass(frozen=True)
class HistoryFacts:
    """What one sign has already used, over the history window."""

    colours: tuple[str, ...] = ()
    numbers: tuple[int, ...] = ()
    combinations: tuple[frozenset[str], ...] = ()
    previous_tags: tuple[str, ...] = ()


def permitted_tag_count(spoken_chars: int) -> tuple[int, int]:
    """How many tags a script of this spoken length may carry.

    Scaled rather than fixed so the rule fits a two-sentence promo and a
    four-hundred-character horoscope alike. Both bounds are clamped to the
    horoscope's figures: no script needs more than MAX_TAGS, and none may fall
    below FLOOR_TAGS, which is an opening and a close.
    """
    ideal = spoken_chars / CHARS_PER_TAG
    low = min(max(round(ideal) - 1, FLOOR_TAGS), MIN_TAGS)
    high = min(max(round(ideal) + 2, low + 1), MAX_TAGS)
    return low, high


def _check_tags(
    item_key: str, facts: ScriptFacts, *, spoken_chars: int | None
) -> list[Violation]:
    out: list[Violation] = []
    for tag in facts.tags:
        if has_devanagari(tag):
            out.append(Violation(
                item_key, "tag_devanagari",
                f"tag [{tag}] is Devanagari; tags must be English delivery directions",
                hard=True,
            ))
        elif "," in tag:
            out.append(Violation(
                item_key, "tag_compound",
                f"tag [{tag}] carries more than one quality; use the dominant one alone",
                hard=True,
            ))
        elif tag.lower() not in ALL_TAGS:
            out.append(Violation(
                item_key, "tag_off_bank",
                f"tag [{tag}] is not in the permitted tag bank",
                hard=True,
            ))

    if spoken_chars is None:
        # The count is derived from the length, so without the text there is
        # nothing to derive it from. The vocabulary checks above still stand.
        return out

    low, high = permitted_tag_count(spoken_chars)
    if not low <= len(facts.tags) <= high:
        # Soft, unlike the vocabulary rules above. A Devanagari bracket reaches
        # the voice engine and breaks the audio; four tags instead of six is
        # only a flatter read, and failing a run over it would make the harness
        # the reason a category stopped working.
        out.append(Violation(
            item_key, "tag_count",
            f"{len(facts.tags)} tags for {spoken_chars} spoken characters; "
            f"expected {low}-{high}",
            hard=False,
        ))
    return out


def _check_number(item_key: str, facts: ScriptFacts, *, expected: bool) -> list[Violation]:
    if facts.number is None:
        if not expected:
            return []
        return [Violation(
            item_key, "number_missing",
            "no जादुई अंक found — the line must read 'जादुई अंक: <words> (<digits>)'",
            hard=True,
        )]
    if not MIN_NUMBER <= facts.number <= MAX_NUMBER:
        return [Violation(
            item_key, "number_range",
            f"जादुई अंक {facts.number} is outside {MIN_NUMBER}-{MAX_NUMBER}",
            hard=True,
        )]
    return []


def _check_against_history(
    item_key: str, facts: ScriptFacts, history: HistoryFacts
) -> list[Violation]:
    out: list[Violation] = []

    if facts.colour:
        clash = next((c for c in history.colours if colours_collide(facts.colour, c)), None)
        if clash is not None:
            out.append(Violation(
                item_key, "colour_reused",
                f"शुभ रंग '{facts.colour}' repeats '{clash}' from a recent day",
                hard=True,
            ))
    if facts.number is not None and facts.number in history.numbers:
        out.append(Violation(
            item_key, "number_reused",
            f"जादुई अंक {facts.number} was already used for this sign recently",
            hard=True,
        ))
    if facts.combination and facts.combination in history.combinations:
        out.append(Violation(
            item_key, "areas_reused",
            f"the area combination {', '.join(sorted(facts.combination))} was used recently",
            hard=False,
        ))
    if facts.tags and tuple(facts.tags) == history.previous_tags:
        out.append(Violation(
            item_key, "tags_repeat_previous_day",
            "the tag sequence is identical to this sign's previous day",
            hard=False,
        ))
    return out


# Below this a shared fragment is a turn of phrase, not a reused sentence.
# "आज का दिन शुभ है।" is eight words and genuinely repetitive; a four-word
# clause two signs happen to share is not worth a complaint.
MIN_SHARED_SENTENCE_CHARS = 25

# Word-shingle overlap at or above this reads as one script reworded into
# another rather than two scripts that happen to share some vocabulary. Set for
# the soft flag it feeds: high enough that distinct scripts (which share little
# at three-word granularity) stay clear, low enough that a reworded copy with a
# colour and a few words changed still trips it. Both directions of sameness use
# it — twelve signs written alike on one day, and one sign written like its own
# recent days — because to a viewer they are the same complaint.
SIMILARITY_SHINGLE_N = 3
SIMILARITY_THRESHOLD = 0.5


def _sentences(script: str) -> set[str]:
    """The script's sentences, normalised for comparison.

    Split on the danda and the Latin sentence enders, because a script written
    in Devanagari still picks up "!" in its hook.
    """
    spoken = strip_tags(script)
    parts = re.split(r"[।!?.]+", spoken)
    return {
        " ".join(part.split())
        for part in parts
        if len(" ".join(part.split())) >= MIN_SHARED_SENTENCE_CHARS
    }


def _check_shared_sentences(raw: dict[str, str]) -> list[Violation]:
    """Sentences appearing word-for-word in more than one of the day's scripts.

    The brief used to pin one sentence verbatim — the health line — so it ran
    identically in all twelve scripts every day. That was the single guaranteed
    duplicate in a system built to remove duplicates, and the rules stepped
    around it because nothing looked. The brief no longer asks for it; this is
    what stops it coming back, whether by instruction or by the model settling
    into a formula on a beat that invites one.

    Soft: a shared sentence is dull, not broken, and failing a paid run over a
    turn of phrase two signs happen to share would be worse than the repetition.

    Same-day only. Repetition across days is already covered by the prose
    window, which quotes recent scripts back with "do not reuse their phrasing".
    """
    owners: dict[str, list[str]] = {}
    for item_key, script in raw.items():
        for sentence in _sentences(script):
            owners.setdefault(sentence, []).append(item_key)

    out: list[Violation] = []
    for sentence, keys in owners.items():
        if len(keys) < 2:
            continue
        excerpt = sentence if len(sentence) <= 60 else sentence[:57] + "…"
        # Reported against every sharer: none of them is more at fault than the
        # others, and the operator wants to see the whole set that matched.
        for item_key in keys:
            out.append(Violation(
                item_key, "shared_sentence",
                f'"{excerpt}" appears word-for-word in {len(keys)} of today\'s scripts',
                hard=False,
            ))
    return out


def _check_similar_same_day(raw: dict[str, str]) -> list[Violation]:
    """Pairs of the day's scripts that reword the same content.

    Verbatim-sentence sharing is already caught by ``_check_shared_sentences``;
    this catches the case that slips past it — two scripts that say the same
    thing in almost the same words, a colour or a name apart. With only two
    themes to write about, twelve signs converge on the same beats far more
    readily than they did across five, so this is where that shows up.

    Soft, and reported against both scripts in a matching pair: neither is more
    at fault, and the operator wants to see the whole cluster that reads alike.
    """
    out: list[Violation] = []
    keys = list(raw)
    spoken = {key: strip_tags(text) for key, text in raw.items()}
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            score = similarity(spoken[a], spoken[b], n=SIMILARITY_SHINGLE_N)
            if score < SIMILARITY_THRESHOLD:
                continue
            pct = round(score * 100)
            for owner, other in ((a, b), (b, a)):
                out.append(Violation(
                    owner, "similar_same_day",
                    f"reads {pct}% the same as {other}'s script today — reword it",
                    hard=False,
                ))
    return out


def _check_similar_to_recent(
    item_key: str, spoken: str, recent_texts: list[str]
) -> list[Violation]:
    """Whether a sign's script rewords one of its own recent days.

    The colour and number rules already stop a sign reusing a fact; this stops
    it reusing the whole script with the fact swapped out. Across-day phrasing
    was otherwise only ever *asked* not to repeat, in the prose block of the
    prompt — an instruction nothing checked. Soft: a rewrite is the operator's
    call, and the recent text is a truncated excerpt, so a near-miss is guidance
    rather than grounds to fail a paid run.
    """
    best = max(
        (similarity(spoken, strip_tags(text), n=SIMILARITY_SHINGLE_N)
         for text in recent_texts),
        default=0.0,
    )
    if best < SIMILARITY_THRESHOLD:
        return []
    return [Violation(
        item_key, "similar_recent_day",
        f"reads {round(best * 100)}% the same as this sign's script from a "
        "recent day — reword it",
        hard=False,
    )]


def _check_same_day(parsed: dict[str, ScriptFacts]) -> list[Violation]:
    """Collisions between the twelve signs written for the same date.

    Reported against the *later* of a colliding pair so a repair rewrites one
    script rather than both — rewriting both invites them to collide again.
    """
    out: list[Violation] = []
    seen_colours: list[tuple[str, str]] = []
    seen_numbers: dict[int, str] = {}

    for item_key, facts in parsed.items():
        if facts.colour:
            clash = next((k for k, c in seen_colours if colours_collide(facts.colour, c)), None)
            if clash is not None:
                out.append(Violation(
                    item_key, "colour_collision_today",
                    f"शुभ रंग '{facts.colour}' collides with {clash} today",
                    hard=True,
                ))
            else:
                seen_colours.append((item_key, facts.colour))
        if facts.number is not None:
            if facts.number in seen_numbers:
                out.append(Violation(
                    item_key, "number_collision_today",
                    f"जादुई अंक {facts.number} is already used by "
                    f"{seen_numbers[facts.number]} today",
                    hard=True,
                ))
            else:
                seen_numbers[facts.number] = item_key
    return out


def validate_drafts(
    parsed: dict[str, ScriptFacts],
    *,
    history: dict[str, HistoryFacts],
    truncated: frozenset[str] = frozenset(),
    target_low: int,
    target_high: int,
    raw: dict[str, str] | None = None,
    recent_by_key: dict[str, list[str]] | None = None,
) -> list[Violation]:
    """Every violation across a day's set, hard and soft together.

    :param parsed: Facts per item key, for the whole set being written.
    :param history: What each item key has already used, keyed the same way.
    :param truncated: Item keys whose script had to be trimmed to fit the render
        limit — a script cut at the knees loses its closing line, so this is a
        rewrite, not a warning.
    :param target_low: Lower spoken-length bound, measured tag-stripped.
    :param target_high: Upper spoken-length bound, measured tag-stripped.
    :param raw: The script text per item key, for length checks. Facts alone do
        not carry it.
    :param recent_by_key: Prior days' script text per item key, for the
        across-day near-duplicate check. Absent means no history to compare to.
    """
    out: list[Violation] = []

    # Whether this category's brief asks for a lucky number at all. Inferred
    # from the set rather than configured: a horoscope's twelve scripts all
    # carry one, a one-off promo carries none, and demanding one of the promo
    # would fail every generation that is not a horoscope. If some scripts have
    # one and others do not, the ones without it are the failures.
    #
    # Keyed off the *line* being present, not off a number having been read out
    # of it. Inferring from successful parses alone failed open: one reply that
    # wrote its numbers in a form the parser missed made every script look
    # number-less, which read as "this category has no numbers", and twelve
    # identical lucky numbers shipped with nothing flagged.
    expects_number = any(
        facts.number is not None or facts.has_number_line for facts in parsed.values()
    )

    for item_key, facts in parsed.items():
        spoken = len(strip_tags(raw[item_key])) if raw and item_key in raw else None
        out.extend(_check_tags(item_key, facts, spoken_chars=spoken))
        out.extend(_check_number(item_key, facts, expected=expects_number))
        out.extend(_check_against_history(
            item_key, facts, history.get(item_key, HistoryFacts())
        ))

        if item_key in truncated:
            out.append(Violation(
                item_key, "truncated",
                "script overran the render limit and lost its ending; write it shorter",
                hard=True,
            ))
        elif raw and item_key in raw:
            spoken = len(strip_tags(raw[item_key]))
            if not target_low <= spoken <= target_high:
                out.append(Violation(
                    item_key, "length",
                    f"{spoken} spoken characters; expected {target_low}-{target_high}",
                    hard=False,
                ))

    out.extend(_check_same_day(parsed))
    if raw:
        out.extend(_check_shared_sentences(raw))
        out.extend(_check_similar_same_day(raw))
        if recent_by_key:
            for item_key, text in raw.items():
                recent_texts = recent_by_key.get(item_key)
                if recent_texts:
                    out.extend(_check_similar_to_recent(
                        item_key, strip_tags(text), recent_texts
                    ))
    return out


def hard(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.hard]


def offending_keys(violations: list[Violation]) -> list[str]:
    """The item keys with at least one hard violation, in stable order."""
    seen: list[str] = []
    for violation in violations:
        if violation.hard and violation.item_key not in seen:
            seen.append(violation.item_key)
    return seen
