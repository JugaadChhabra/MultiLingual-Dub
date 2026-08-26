"""Tests for the check that decides whether a day's scripts may be kept.

The behaviours worth pinning: an illegal delivery tag never reaches the voice
engine, a colour or number that repeats within the day or across the window is
caught, the severity split holds so a dull script still ships while a wrong one
does not, and a category that does not use lucky numbers is not asked for one.
"""
from __future__ import annotations

from services.script_parse import parse_script
from services.script_validate import (
    FLOOR_TAGS,
    MAX_TAGS,
    MIN_TAGS,
    HistoryFacts,
    permitted_tag_count,
    hard,
    offending_keys,
    validate_drafts,
)

TARGETS = {"target_low": 1, "target_high": 10_000}


def script(*, colour: str = "नीला", number: str = "१७", tags: tuple[str, ...] = (),
           areas: str = "नई शुरुआत, ऊर्जा और नेतृत्व") -> str:
    """One skeleton-shaped script, with the pieces a test wants to vary."""
    used = tags or ("warm", "reassuring", "optimistic", "calm", "bright", "uplifting")
    fortune = f"[{used[4]}] शुभ रंग: {colour} | जादुई अंक: संख्या ({number})।" if number else ""
    return (
        f"[{used[0]}] मेष राशि के जातकों... आज {areas}!\n\n"
        f"[{used[1]}] एक अवसर मिलेगा। [{used[2]}] दिन अच्छा बीतेगा। "
        f"[{used[3]}] स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी। {fortune}\n\n"
        f"[{used[5]}] आज का दिन शुभ है।"
    )


def check(scripts: dict[str, str], history: dict[str, HistoryFacts] | None = None, **kw):
    return validate_drafts(
        {key: parse_script(text) for key, text in scripts.items()},
        history=history or {},
        raw=scripts,
        **TARGETS,
        **kw,
    )


def rules(violations) -> set[str]:
    return {v.rule for v in violations}


def test_a_clean_set_has_no_hard_violations() -> None:
    scripts = {
        "Aries": script(colour="नीला", number="१७"),
        "Taurus": script(colour="हरा", number="२३"),
    }
    assert hard(check(scripts)) == []


# --- delivery tags ------------------------------------------------------


def test_a_devanagari_tag_is_a_hard_violation() -> None:
    tags = ("सामान्य गति", "reassuring", "optimistic", "calm", "bright", "uplifting")
    found = check({"Aries": script(tags=tags)})
    assert "tag_devanagari" in rules(found)
    assert all(v.hard for v in found if v.rule == "tag_devanagari")


def test_a_two_quality_tag_is_a_hard_violation() -> None:
    tags = ("calm, confident", "reassuring", "optimistic", "calm", "bright", "uplifting")
    assert "tag_compound" in rules(check({"Aries": script(tags=tags)}))


def test_a_tag_outside_the_bank_is_a_hard_violation() -> None:
    tags = ("thrilled", "reassuring", "optimistic", "calm", "bright", "uplifting")
    assert "tag_off_bank" in rules(check({"Aries": script(tags=tags)}))


def test_the_tag_count_is_reported_but_never_blocks() -> None:
    """A flat read is a quality problem; only the vocabulary is a functional one.

    Blocking on count would make the harness the reason one of the operator's
    own categories stopped generating, which is the opposite of the point.
    """
    long_but_untagged = "[warm] " + "एक साधारण वाक्य। " * 30
    found = check({"Aries": long_but_untagged})
    assert "tag_count" in rules(found)
    assert hard(found) == []


def test_the_permitted_tag_count_scales_with_spoken_length() -> None:
    """A one-off promo is not a horoscope and has fewer beats to tag."""
    short_low, short_high = permitted_tag_count(120)
    long_low, long_high = permitted_tag_count(420)
    assert short_low < long_low and short_high < long_high
    assert long_low <= MIN_TAGS and long_high == MAX_TAGS


def test_a_very_short_script_still_permits_a_tag_or_two() -> None:
    low, high = permitted_tag_count(20)
    assert low == FLOOR_TAGS and high > low


def test_no_length_permits_more_tags_than_the_horoscope_needs() -> None:
    assert permitted_tag_count(5_000)[1] == MAX_TAGS


# --- the lucky number ---------------------------------------------------


def test_a_single_digit_number_is_refused() -> None:
    assert "number_range" in rules(check({"Aries": script(number="९")}))


def test_a_three_digit_number_is_refused() -> None:
    assert "number_range" in rules(check({"Aries": script(number="१०५")}))


def test_a_double_digit_number_passes() -> None:
    assert "number_range" not in rules(check({"Aries": script(number="१७")}))


def test_a_category_with_no_numbers_at_all_is_not_asked_for_one() -> None:
    """A one-off promo has no fortune line, and must not fail for lacking one."""
    assert "number_missing" not in rules(check({"promo": script(number="")}))
    assert hard(check({"promo": script(number="")})) == []


def test_a_missing_number_is_refused_when_the_rest_of_the_set_has_one() -> None:
    found = check({"Aries": script(number="१७"), "Taurus": script(number="", colour="हरा")})
    assert "number_missing" in rules(found)
    assert offending_keys(hard(found)) == ["Taurus"]


# --- collisions within one day ------------------------------------------


def test_two_signs_sharing_a_colour_collide() -> None:
    found = check({"Aries": script(colour="नीला", number="१७"),
                   "Taurus": script(colour="नीला", number="२३")})
    assert "colour_collision_today" in rules(found)


def test_a_shade_of_a_colour_already_used_today_collides() -> None:
    found = check({"Aries": script(colour="रूबी रेड", number="१७"),
                   "Taurus": script(colour="गहरा रूबी रेड", number="२३")})
    assert "colour_collision_today" in rules(found)


def test_two_signs_sharing_a_number_collide() -> None:
    found = check({"Aries": script(colour="नीला", number="१७"),
                   "Taurus": script(colour="हरा", number="१७")})
    assert "number_collision_today" in rules(found)


def test_only_the_later_of_a_colliding_pair_is_rewritten() -> None:
    """Rewriting both invites them to collide again."""
    found = check({"Aries": script(colour="नीला", number="१७"),
                   "Taurus": script(colour="नीला", number="२३")})
    assert offending_keys(hard(found)) == ["Taurus"]


# --- collisions against history -----------------------------------------


def test_a_colour_used_recently_by_this_sign_is_refused() -> None:
    found = check(
        {"Aries": script(colour="गहरा रूबी रेड", number="१७")},
        {"Aries": HistoryFacts(colours=("रूबी रेड",))},
    )
    assert "colour_reused" in rules(found)


def test_a_colour_used_recently_by_a_different_sign_is_fine() -> None:
    """Per sign, not per set: a viewer of मेष never sees कन्या's colour."""
    found = check(
        {"Aries": script(colour="रूबी रेड", number="१७")},
        {"Taurus": HistoryFacts(colours=("रूबी रेड",))},
    )
    assert "colour_reused" not in rules(found)


def test_a_number_used_recently_by_this_sign_is_refused() -> None:
    found = check(
        {"Aries": script(number="१७")},
        {"Aries": HistoryFacts(numbers=(17,))},
    )
    assert "number_reused" in rules(found)


# --- severity -----------------------------------------------------------


def test_a_reused_area_combination_is_soft() -> None:
    """A duller script still ships; only a wrong one is rewritten."""
    found = check(
        {"Aries": script()},
        {"Aries": HistoryFacts(combinations=(frozenset({"नई शुरुआत", "ऊर्जा", "नेतृत्व"}),))},
    )
    assert "areas_reused" in rules(found)
    assert hard(found) == []


def test_repeating_yesterdays_tag_sequence_is_soft() -> None:
    found = check(
        {"Aries": script()},
        {"Aries": HistoryFacts(
            previous_tags=("warm", "reassuring", "optimistic", "calm", "bright", "uplifting")
        )},
    )
    assert "tags_repeat_previous_day" in rules(found)
    assert hard(found) == []


def test_length_drift_is_soft() -> None:
    found = validate_drafts(
        {"Aries": parse_script(script())},
        history={},
        raw={"Aries": script()},
        target_low=380,
        target_high=460,
    )
    assert "length" in rules(found)
    assert hard(found) == []


def test_length_is_measured_without_the_tags() -> None:
    """Counting tags against a spoken-length budget would shrink the script."""
    text = script()
    spoken = validate_drafts(
        {"Aries": parse_script(text)},
        history={},
        raw={"Aries": text},
        target_low=1,
        target_high=len(text) - 1,
    )
    assert "length" not in rules(spoken)


def test_a_truncated_script_is_a_hard_violation() -> None:
    found = check({"Aries": script()}, truncated=frozenset({"Aries"}))
    assert "truncated" in rules(found)
    assert offending_keys(hard(found)) == ["Aries"]


def test_a_set_whose_numbers_are_all_unparseable_is_not_let_through() -> None:
    """The dangerous direction of the set-level inference.

    Reading "does this category use numbers?" off successful parses alone means
    a reply the parser cannot read looks like a category that has no numbers —
    so every number rule switches itself off and twelve identical lucky numbers
    ship with nothing flagged.
    """
    scripts = {
        f"S{i}": script(colour=c, number="").replace(
            "स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी।",
            "स्वास्थ्य उत्तम रहेगा। [bright] शुभ रंग: " + c + " | जादुई अंक: सत्रह।",
        )
        for i, c in enumerate(["नीला", "हरा", "पीला", "गुलाबी"])
    }
    found = check(scripts)
    assert "number_missing" in rules(found)
    assert len(offending_keys(hard(found))) == len(scripts)


def test_latin_digits_are_read_so_collisions_are_still_caught() -> None:
    found = check({"Aries": script(colour="नीला", number="17"),
                   "Taurus": script(colour="हरा", number="17")})
    assert "number_collision_today" in rules(found)


# --- shared sentences ---------------------------------------------------


HEALTH_LINE = "स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी"


def test_the_old_verbatim_health_line_would_now_be_caught() -> None:
    """The brief used to pin this sentence, so it ran identically in all twelve
    scripts every day — the one guaranteed duplicate in a system built to
    remove duplicates. The brief no longer asks for it; this is what stops it
    coming back."""
    scripts = {
        "Aries": script(colour="नीला", number="१७", areas="नई शुरुआत, ऊर्जा और नेतृत्व"),
        "Taurus": script(colour="हरा", number="२३", areas="धन, रिश्ते और धैर्य"),
    }
    found = check(scripts)
    shared = [v for v in found if v.rule == "shared_sentence"]
    assert shared, "an identical sentence across two signs must be reported"
    assert HEALTH_LINE[:20] in shared[0].detail
    assert {v.item_key for v in shared} == {"Aries", "Taurus"}


def test_a_shared_sentence_is_soft() -> None:
    """Dull, not broken. Failing a paid run over a shared turn of phrase would
    be worse than the repetition."""
    found = check({"Aries": script(colour="नीला", number="१७"),
                   "Taurus": script(colour="हरा", number="२३", areas="धन, रिश्ते और धैर्य")})
    assert hard([v for v in found if v.rule == "shared_sentence"]) == []


def test_distinct_health_lines_are_not_reported() -> None:
    a = script(colour="नीला", number="१७", areas="नई शुरुआत, ऊर्जा और नेतृत्व").replace(
        HEALTH_LINE, "सेहत को लेकर आज मन हल्का और स्थिर बना रहेगा")
    b = script(colour="हरा", number="२३", areas="धन, रिश्ते और धैर्य").replace(
        HEALTH_LINE, "आज शरीर में स्फूर्ति और भीतर गहरी शांति महसूस होगी")
    assert [v for v in check({"A": a, "B": b}) if v.rule == "shared_sentence"] == []


def test_a_short_shared_clause_is_not_worth_a_complaint() -> None:
    """Two signs sharing four words is a turn of phrase, not a reused sentence."""
    found = check({"A": "[warm] राशि के जातकों... आज एक, दो और तीन! शुभ है। "
                        "[reassuring] कुछ और। [calm] ठीक। [bright] शुभ रंग: नीला | "
                        "जादुई अंक: सत्रह (१७)। [uplifting] बढ़ते रहें।",
                   "B": "[warm] राशि के जातकों... आज चार, पाँच और छह! शुभ है। "
                        "[reassuring] अलग बात। [calm] बढ़िया। [bright] शुभ रंग: हरा | "
                        "जादुई अंक: तेईस (२३)। [uplifting] आगे बढ़ें।"})
    assert [v for v in found if v.rule == "shared_sentence"] == []


def test_two_signs_sharing_a_hook_is_also_a_shared_sentence() -> None:
    """Each sign draws from its own pool, so identical areas across signs is a
    duplicate worth surfacing in its own right."""
    found = check({"Aries": script(colour="नीला", number="१७"),
                   "Taurus": script(colour="हरा", number="२३")})
    details = [v.detail for v in found if v.rule == "shared_sentence"]
    assert any("नई शुरुआत" in d for d in details)
