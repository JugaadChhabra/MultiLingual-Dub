"""Tests for filename collision handling.

The sheet's audio_type column is not unique, so two rows in one activity
routinely ask for the same filename. Before this was its own module, proving
that the second one does not silently overwrite the first meant running an
entire batch job.
"""
from __future__ import annotations

from batch.activity import ActivityBuffer, build_output_filename, dedupe_filename
from batch.models import ExcelRow


def _row(index: int, audio_type: str = "promo") -> ExcelRow:
    return ExcelRow(row_index=index, text=f"row{index}", emotion="", activity_name="Act", audio_type=audio_type)


# --- filename construction -------------------------------------------------


def test_the_audio_type_becomes_the_filename() -> None:
    assert build_output_filename(audio_type="Intro", row_index=2, language="hi-IN") == "Intro.mp3"


def test_an_existing_mp3_extension_is_not_doubled() -> None:
    assert build_output_filename(audio_type="Intro.mp3", row_index=2, language="hi-IN") == "Intro.mp3"
    assert build_output_filename(audio_type="Intro.MP3", row_index=2, language="hi-IN") == "Intro.MP3"


def test_a_blank_audio_type_falls_back_to_row_and_language() -> None:
    assert build_output_filename(audio_type="", row_index=7, language="ta-IN") == "row-7-ta-IN.mp3"


# --- dedupe ----------------------------------------------------------------


def test_an_unused_name_is_left_alone() -> None:
    assert dedupe_filename("a.mp3", {}, 2) == ("a.mp3", False)


def test_a_taken_name_gains_the_row_index() -> None:
    assert dedupe_filename("a.mp3", {"a.mp3": b""}, 3) == ("a-row3.mp3", True)


def test_a_taken_name_and_row_suffix_gains_a_counter() -> None:
    existing = {"a.mp3": b"", "a-row3.mp3": b""}

    assert dedupe_filename("a.mp3", existing, 3) == ("a-row3-2.mp3", True)


# --- the buffer ------------------------------------------------------------


def test_audio_is_filed_under_its_language() -> None:
    buffer = ActivityBuffer(["hi-IN", "ta-IN"])

    buffer.add(_row(2), "hi-IN", b"hindi-audio")
    buffer.add(_row(2), "ta-IN", b"tamil-audio")

    assert buffer.files["hi-IN"] == {"promo.mp3": b"hindi-audio"}
    assert buffer.files["ta-IN"] == {"promo.mp3": b"tamil-audio"}


def test_two_rows_sharing_an_audio_type_do_not_overwrite_each_other() -> None:
    """The bug this module exists to make testable."""
    buffer = ActivityBuffer(["hi-IN"])

    first = buffer.add(_row(2, "promo"), "hi-IN", b"first")
    second = buffer.add(_row(3, "promo"), "hi-IN", b"second")

    assert first == "promo.mp3"
    assert second == "promo-row3.mp3"
    assert buffer.files["hi-IN"] == {"promo.mp3": b"first", "promo-row3.mp3": b"second"}
    assert buffer.collisions_resolved == 1


def test_three_rows_sharing_an_audio_type_all_survive() -> None:
    buffer = ActivityBuffer(["hi-IN"])

    buffer.add(_row(2, "promo"), "hi-IN", b"a")
    buffer.add(_row(3, "promo"), "hi-IN", b"b")
    buffer.add(_row(3, "promo"), "hi-IN", b"c")

    assert len(buffer.files["hi-IN"]) == 3
    assert buffer.collisions_resolved == 2


def test_the_same_name_in_different_languages_is_not_a_collision() -> None:
    buffer = ActivityBuffer(["hi-IN", "ta-IN"])

    buffer.add(_row(2, "promo"), "hi-IN", b"a")
    buffer.add(_row(2, "promo"), "ta-IN", b"b")

    assert buffer.collisions_resolved == 0
    assert list(buffer.files["hi-IN"]) == ["promo.mp3"]
    assert list(buffer.files["ta-IN"]) == ["promo.mp3"]


def test_a_language_not_declared_up_front_is_still_accepted() -> None:
    """Retry passes only the failed languages, so add() must not assume the
    buffer was seeded with them."""
    buffer = ActivityBuffer(["hi-IN"])

    buffer.add(_row(2), "ta-IN", b"audio")

    assert buffer.files["ta-IN"] == {"promo.mp3": b"audio"}


def test_a_fresh_buffer_is_empty() -> None:
    buffer = ActivityBuffer(["hi-IN", "ta-IN"])

    assert buffer.is_empty
    assert buffer.total_files() == 0

    buffer.add(_row(2), "hi-IN", b"audio")

    assert not buffer.is_empty
    assert buffer.total_files() == 1


def test_expected_filename_ignores_what_is_already_buffered() -> None:
    """Append mode compares against the REMOTE zip, so this must be the name a
    row would take on a clean run — not a deduped one."""
    buffer = ActivityBuffer(["hi-IN"])
    buffer.add(_row(2, "promo"), "hi-IN", b"a")

    assert buffer.expected_filename(_row(3, "promo"), "hi-IN") == "promo.mp3"
