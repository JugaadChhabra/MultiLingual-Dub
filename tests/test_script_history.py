"""Tests for the generated-script history — the defence against repetition."""
from __future__ import annotations

from pathlib import Path

from services.script_history import ScriptHistoryStore
from services.script_writer import DraftScript


def _drafts(*titles: str) -> list[DraftScript]:
    return [DraftScript(title=t, script=f"script for {t}") for t in titles]


def test_records_and_reads_back_recent_days(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-15", category="horoscope", language="hi-IN",
        drafts=_drafts("Aries"),
    )
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=_drafts("Taurus"),
    )

    recent = store.recent(category="horoscope", language="hi-IN")

    assert [d.script for d in recent] == ["script for Taurus", "script for Aries"]
    # The date travels in the title so the writer can see how far back a phrasing was used.
    assert recent[0].title == "2026-08-16 Taurus"


def test_the_day_being_regenerated_is_not_quoted_back_at_itself(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-17", category="horoscope", language="hi-IN",
        drafts=_drafts("Aries"),
    )

    assert store.recent(category="horoscope", language="hi-IN", before="2026-08-17") == []


def test_regenerating_a_day_overwrites_rather_than_accumulating(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=_drafts("Aries"),
    )
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[DraftScript(title="Aries", script="a second attempt")],
    )

    recent = store.recent(category="horoscope", language="hi-IN")
    assert [d.script for d in recent] == ["a second attempt"]


def test_other_categories_and_languages_are_separate_histories(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=_drafts("Aries"),
    )

    assert store.recent(category="festival", language="hi-IN") == []
    assert store.recent(category="horoscope", language="en-IN") == []


def test_only_the_last_n_days_are_returned(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    for day in range(1, 13):
        store.record(
            publish_date=f"2026-08-{day:02d}", category="horoscope", language="hi-IN",
            drafts=_drafts("Aries"),
        )

    assert len(store.recent(category="horoscope", language="hi-IN", days=7)) == 7


def test_long_scripts_are_excerpted_so_they_do_not_dominate_the_prompt(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[DraftScript(title="Aries", script="x" * 900)],
    )

    recent = store.recent(category="horoscope", language="hi-IN", excerpt_chars=100)
    assert len(recent[0].script) == 101 and recent[0].script.endswith("…")


# --- structured facts ---------------------------------------------------

def _tagged(item_key: str, *, colour: str, number: str, areas: str) -> DraftScript:
    """A skeleton-shaped draft, so recording it parses out facts."""
    return DraftScript(
        title=item_key,
        key=item_key,
        script=(
            f"[warm] मेष राशि के जातकों... आज {areas}!\n\n"
            "[reassuring] एक अवसर मिलेगा। [optimistic] दिन अच्छा बीतेगा। "
            "[calm] स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी। "
            f"[bright] शुभ रंग: {colour} | जादुई अंक: संख्या ({number})।\n\n"
            "[uplifting] आज का दिन शुभ है।"
        ),
    )


def test_facts_are_parsed_out_at_record_time(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[_tagged("Aries", colour="रूबी रेड", number="१७", areas="ऊर्जा, साहस और नेतृत्व")],
    )

    facts = store.facts(category="horoscope", language="hi-IN")
    assert facts["Aries"].colours == ("रूबी रेड",)
    assert facts["Aries"].numbers == (17,)
    assert facts["Aries"].combinations == (frozenset({"ऊर्जा", "साहस", "नेतृत्व"}),)
    assert facts["Aries"].previous_tags[0] == "warm"


def test_facts_are_grouped_per_item_not_per_category(tmp_path: Path) -> None:
    """A colour repeating for मेष is what a viewer of मेष notices."""
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[
            _tagged("Aries", colour="नीला", number="१७", areas="ऊर्जा, साहस और नेतृत्व"),
            _tagged("Taurus", colour="हरा", number="२३", areas="धन, रिश्ते और धैर्य"),
        ],
    )

    facts = store.facts(category="horoscope", language="hi-IN")
    assert facts["Aries"].colours == ("नीला",)
    assert facts["Taurus"].colours == ("हरा",)


def test_facts_accumulate_across_days(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    for day, (colour, number) in enumerate(
        [("नीला", "१७"), ("हरा", "२३"), ("पीला", "३१")], start=14
    ):
        store.record(
            publish_date=f"2026-08-{day}", category="horoscope", language="hi-IN",
            drafts=[_tagged("Aries", colour=colour, number=number, areas="ऊर्जा, साहस और नेतृत्व")],
        )

    facts = store.facts(category="horoscope", language="hi-IN")
    assert set(facts["Aries"].colours) == {"नीला", "हरा", "पीला"}
    assert set(facts["Aries"].numbers) == {17, 23, 31}


def test_previous_tags_are_the_most_recent_days(tmp_path: Path) -> None:
    """Only the last day's tag sequence matters to the repeat rule."""
    store = ScriptHistoryStore(tmp_path)
    for day in ("14", "15"):
        store.record(
            publish_date=f"2026-08-{day}", category="horoscope", language="hi-IN",
            drafts=[_tagged("Aries", colour=f"रंग{day}", number="१७", areas="ऊर्जा, साहस और नेतृत्व")],
        )

    facts = store.facts(category="horoscope", language="hi-IN")
    assert facts["Aries"].previous_tags == (
        "warm", "reassuring", "optimistic", "calm", "bright", "uplifting"
    )


def test_the_day_being_regenerated_is_excluded_from_facts(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[_tagged("Aries", colour="नीला", number="१७", areas="ऊर्जा, साहस और नेतृत्व")],
    )

    facts = store.facts(category="horoscope", language="hi-IN", before="2026-08-16")
    assert facts == {}


def test_records_written_before_facts_existed_are_skipped(tmp_path: Path) -> None:
    """No item key means no safe grouping — a guess would forbid the wrong sign's
    colour. The window heals as new days are written."""
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[DraftScript(title="Aries", script="कोई ढाँचा नहीं।")],
    )

    assert store.facts(category="horoscope", language="hi-IN") == {}
    # The prose window still returns it, so nothing is lost for phrasing.
    assert len(store.recent(category="horoscope", language="hi-IN")) == 1


def test_a_script_that_ignores_the_skeleton_records_without_facts(tmp_path: Path) -> None:
    store = ScriptHistoryStore(tmp_path)
    store.record(
        publish_date="2026-08-16", category="horoscope", language="hi-IN",
        drafts=[DraftScript(title="Aries", key="Aries", script="बस एक वाक्य।")],
    )

    facts = store.facts(category="horoscope", language="hi-IN")
    assert facts == {} or facts["Aries"].colours == ()
