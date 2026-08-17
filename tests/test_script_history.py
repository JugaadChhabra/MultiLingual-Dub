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
