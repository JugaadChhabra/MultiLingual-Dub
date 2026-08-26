"""Reading back what the model wrote, so it can be checked against history.

The script writer asks for a fixed skeleton — a hook naming three areas, a body
ending in a colour and a lucky number, a closing line, with delivery tags in
square brackets. This module turns that skeleton back into fields.

Everything here is best-effort and never raises on malformed input. A script
that does not match the skeleton yields empty facts, and the validator refuses
it on that basis; a parse error must never be the thing that loses an otherwise
good draft.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

# Devanagari digits ०-९ sit at U+0966-U+096F; the block as a whole at U+0900.
DEVANAGARI_DIGITS = "०१२३४५६७८९"
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_TAG = re.compile(r"\[([^\]\n]*)\]")
# The hook's three areas: everything between "आज" and the exclamation that ends
# the line. Read from the first paragraph only, because "आज" recurs freely in
# the body.
#
# The run is a bounded negated class rather than a lazy ".+?". Lazy-dot against
# a trailing literal backtracks from every "आज" in the subject, and since
# strip_tags collapses newlines a reply with no blank line and no "!" becomes
# one long subject — 96 kB of "आज " took 15 seconds to fail to match. A class
# that cannot cross "!" has nothing to backtrack over.
_HOOK_AREAS = re.compile(r"आज\s+([^!]{1,300})!")
_COLOUR_AND_NUMBER = re.compile(
    r"शुभ\s*रंग\s*:\s*(?P<colour>[^|]{1,120}?)\s*\|\s*जादुई\s*अंक\s*:\s*(?P<number>[^।\n]{1,80})"
)
# Whether the script carries a lucky-number line at all, regardless of whether a
# number could be read out of it. The two are different questions: a category
# that never uses numbers is fine, a category that asks for one and produced
# something unreadable is not.
_NUMBER_LABEL = re.compile(r"जादुई\s*अंक\s*:")
# The digits the number line carries in brackets, e.g. "सत्रह (१७)".
#
# Latin digits are accepted as well as Devanagari. The brief asks for Devanagari
# and the model normally obliges, but reading only those made the uniqueness
# rules fail *open*: one reply written "(17)" parsed every sign to None, which
# the set-level inference then read as "this category has no lucky numbers", and
# twelve identical numbers shipped unflagged.
_BRACKETED_DIGITS = re.compile(rf"[(（]\s*([{DEVANAGARI_DIGITS}0-9]+)\s*[)）]")

_AREA_SEPARATORS = re.compile(r"\s*(?:,|।|\sऔर\s)\s*")

# Shade modifiers, stripped before two colours are compared. "गहरा रूबी रेड" and
# "रूबी रेड" are the same colour to a viewer, so they must be the same colour to
# the uniqueness check.
COLOUR_MODIFIERS = (
    "गहरा", "गहरी", "गाढ़ा", "गाढ़ी", "हल्का", "हल्की",
    "चमकीला", "चमकीली", "फीका", "फीकी", "उजला", "गहन",
)


@dataclass(frozen=True)
class ScriptFacts:
    """The checkable parts of one script.

    Absent fields mean "the skeleton did not yield this", not "the model chose
    nothing" — the two are indistinguishable from the text and both are the
    validator's problem, not this module's.
    """

    areas: tuple[str, ...] = ()
    colour: str | None = None
    number: int | None = None
    tags: tuple[str, ...] = ()
    has_number_line: bool = False

    @property
    def combination(self) -> frozenset[str]:
        """The areas as a set — naming order is surface variety, not identity."""
        return frozenset(self.areas)


def strip_tags(script: str) -> str:
    """The script as it will be heard, with delivery directions removed.

    Length is budgeted against this rather than the raw string: the tags are
    instructions to the speech model, never spoken, and counting them would
    quietly shrink the spoken content every time the tag system grew.
    """
    return re.sub(r"\s+", " ", _TAG.sub(" ", script)).strip()


# A lucky number is two digits. Anything past this is not a number that got
# slightly out of range, it is a parse that went wrong — and int() on a long
# enough run raises outright, since Python caps integer-string conversion at
# 4300 digits.
MAX_NUMBER_DIGITS = 6


def devanagari_to_int(digits: str) -> int | None:
    """Convert a run of digits to an int, or None if it is not one.

    Latin digits count too: the brief asks for Devanagari, but refusing to read
    Latin ones made a mis-written number indistinguishable from no number, and
    the rules that depend on it quietly stopped applying.
    """
    text = digits.strip()
    if not text or len(text) > MAX_NUMBER_DIGITS:
        return None
    out = ""
    for char in text:
        index = DEVANAGARI_DIGITS.find(char)
        if index < 0:
            if not char.isascii() or not char.isdigit():
                return None
            index = int(char)
        out += str(index)
    return int(out) if out else None


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text))


def parse_tags(script: str) -> tuple[str, ...]:
    """Every bracket's contents, in document order, including invalid ones.

    Invalid brackets are returned rather than filtered so the validator can
    name what was wrong. Filtering here would make a Devanagari tag look like a
    missing tag, which is a different and less useful complaint.
    """
    return tuple(match.group(1).strip() for match in _TAG.finditer(script))


def normalise_colour(colour: str) -> str:
    """A colour reduced to what makes it that colour, for comparison."""
    words = [word for word in strip_tags(colour).split() if word]
    kept = [word for word in words if word not in COLOUR_MODIFIERS]
    return " ".join(kept or words).strip().lower()


def colours_collide(left: str, right: str) -> bool:
    """Whether two colours are the same colour, allowing for shade wording.

    Containment rather than equality, so a modifier this module has not seen
    still collides: "रूबी रेड" is contained in "गहरा रूबी रेड".

    Containment of *whole words*, not of substrings. Devanagari colour names
    nest inside one another as text — "हरा" (green) is a substring of "सुनहरा"
    (golden), and "दूरी" of "सिंदूरी" — so a substring test makes unrelated
    colours collide and costs a repair round every time the model picks a
    perfectly good name. Both appeared in a single live run.
    """
    a = set(normalise_colour(left).split())
    b = set(normalise_colour(right).split())
    if not a or not b:
        return False
    return a <= b or b <= a


def parse_script(script: str) -> ScriptFacts:
    """Pull the checkable fields out of one script. Never raises."""
    text = script.strip()
    if not text:
        return ScriptFacts()

    first_paragraph = text.split("\n\n", 1)[0]
    areas: tuple[str, ...] = ()
    hook = _HOOK_AREAS.search(strip_tags(first_paragraph))
    if hook:
        parts = [part.strip() for part in _AREA_SEPARATORS.split(hook.group(1))]
        areas = tuple(part for part in parts if part)

    spoken = strip_tags(text)
    colour: str | None = None
    number: int | None = None
    line = _COLOUR_AND_NUMBER.search(spoken)
    if line:
        colour = line.group("colour").strip() or None
        digits = _BRACKETED_DIGITS.search(line.group("number"))
        if digits:
            number = devanagari_to_int(digits.group(1))

    return ScriptFacts(
        areas=areas,
        colour=colour,
        number=number,
        tags=parse_tags(text),
        has_number_line=bool(_NUMBER_LABEL.search(spoken)),
    )
