"""Tests for reading a script back into checkable fields.

The behaviour worth pinning: the skeleton's areas, colour and number come back
out of it, tags are read verbatim so the validator can complain about the bad
ones, and nothing here ever raises — a script that does not match the skeleton
must degrade to empty facts rather than lose the draft.
"""
from __future__ import annotations

import pytest

from services.script_parse import (
    ScriptFacts,
    colours_collide,
    devanagari_to_int,
    normalise_colour,
    parse_script,
    strip_tags,
)

GOOD = (
    "[warm] मेष राशि के जातकों... आज नई शुरुआत, ऊर्जा और नेतृत्व!\n\n"
    "[reassuring] एक अवसर मिलेगा। "
    "[calm] स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी। "
    "[bright] शुभ रंग: गहरा रूबी रेड | जादुई अंक: सत्रह (१७)।\n\n"
    "[uplifting] आज का दिन शुभ है।"
)


def test_the_three_areas_come_out_of_the_hook() -> None:
    assert parse_script(GOOD).areas == ("नई शुरुआत", "ऊर्जा", "नेतृत्व")


def test_the_colour_and_number_come_out_of_the_fortune_line() -> None:
    facts = parse_script(GOOD)
    assert facts.colour == "गहरा रूबी रेड"
    assert facts.number == 17


def test_tags_come_out_in_order_including_invalid_ones() -> None:
    """Invalid tags are returned, not filtered.

    Filtering would make a Devanagari tag indistinguishable from a missing one,
    and those deserve different complaints.
    """
    script = "[warm] एक। [सामान्य गति] दो। [calm, confident] तीन।"
    assert parse_script(script).tags == ("warm", "सामान्य गति", "calm, confident")


def test_naming_order_does_not_change_the_combination() -> None:
    a = parse_script(GOOD)
    b = parse_script(GOOD.replace("नई शुरुआत, ऊर्जा और नेतृत्व", "नेतृत्व, ऊर्जा और नई शुरुआत"))
    assert a.combination == b.combination


def test_a_script_that_ignores_the_skeleton_yields_empty_facts() -> None:
    facts = parse_script("बस एक साधारण वाक्य, बिना किसी ढाँचे के।")
    assert facts == ScriptFacts(areas=(), colour=None, number=None, tags=())


def test_empty_input_does_not_raise() -> None:
    assert parse_script("   ") == ScriptFacts()


def test_a_missing_number_is_none_rather_than_an_error() -> None:
    assert parse_script(GOOD.replace(" (१७)", "")).number is None


def test_strip_tags_leaves_only_what_is_spoken() -> None:
    spoken = strip_tags(GOOD)
    assert "[" not in spoken and "warm" not in spoken
    assert "मेष राशि के जातकों" in spoken
    # Length is budgeted against this, so the tags must not be counted.
    assert len(spoken) < len(GOOD)


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("१७", 17), ("०९", 9), ("९९", 99), ("१०५", 105),
        # Latin digits count. Refusing them made a mis-written number look like
        # no number at all, which switched the uniqueness rules off entirely.
        ("17", 17), ("105", 105),
        ("", None), ("सत्रह", None), ("1७", 17),
        # A run this long is a parse gone wrong, not a number — and int() raises
        # outright past 4300 digits.
        ("१" * 5000, None),
    ],
)
def test_digits_convert(digits: str, expected: int | None) -> None:
    assert devanagari_to_int(digits) == expected


def test_a_latin_written_number_still_switches_the_rules_on() -> None:
    """The brief asks for Devanagari, but a reply that ignores it must not make
    the whole set look number-less — that failed open and shipped twelve
    identical lucky numbers."""
    facts = parse_script("शुभ रंग: लाल | जादुई अंक: सत्रह (17)।")
    assert facts.number == 17 and facts.has_number_line is True


def test_an_unreadable_number_still_reports_the_line_exists() -> None:
    """"There is no number here" and "this category has no numbers" are
    different answers, and only one of them is safe to act on."""
    facts = parse_script("शुभ रंग: लाल | जादुई अंक: सत्रह।")
    assert facts.number is None and facts.has_number_line is True
    assert parse_script("बस एक वाक्य।").has_number_line is False


def test_a_shade_modifier_is_not_part_of_the_colour() -> None:
    assert normalise_colour("गहरा रूबी रेड") == normalise_colour("रूबी रेड")


def test_a_colour_that_is_only_a_modifier_survives_normalisation() -> None:
    """Stripping every word would make two unrelated colours identical."""
    assert normalise_colour("गहरा") == "गहरा"


@pytest.mark.parametrize(
    ("left", "right", "collides"),
    [
        ("गहरा रूबी रेड", "रूबी रेड", True),
        ("रूबी रेड", "गहरा रूबी रेड", True),
        ("नीला", "नीला", True),
        ("नीला", "हरा", False),
        ("गहरा नीला", "हल्का हरा", False),
    ],
)
def test_colour_collisions(left: str, right: str, collides: bool) -> None:
    assert colours_collide(left, right) is collides


def test_a_colour_name_nested_inside_another_word_does_not_collide() -> None:
    """Devanagari colour names nest as text: "हरा" (green) sits inside "सुनहरा"
    (golden), and "दूरी" inside "सिंदूरी". A substring test made unrelated
    colours collide and cost a repair round; both appeared in one live run."""
    assert colours_collide("सुनहरा", "हरा") is False
    assert colours_collide("सिंदूरी", "दूरी") is False
    assert colours_collide("गहरा", "हरा") is False


def test_a_shade_of_the_same_colour_still_collides() -> None:
    """The whole point of containment — it must survive the narrowing above."""
    assert colours_collide("गहरा रूबी रेड", "रूबी रेड") is True
    assert colours_collide("समुद्री हरा", "हरा") is True


def test_a_pathological_reply_does_not_hang_the_parser() -> None:
    """A lazy ".+?" against a trailing literal backtracks from every "आज".

    strip_tags collapses newlines, so a reply with no blank line and no "!"
    becomes one long subject: 96 kB of "आज " took 15 seconds to fail to match,
    inside the request thread.
    """
    import time

    start = time.monotonic()
    parse_script("आज " * 32_000)
    assert time.monotonic() - start < 1.0
