"""Video job intake: a single submission's raw form values in, a spec out.

The rules here used to live only in the Video pane. A request that skipped the
pane (a script, a stale tab, a curl) got neither, and the title becomes the NAS
filename while a script over the writer's limit is one HeyGen will not render.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from services.script_writer import MAX_SCRIPT_CHARS
from services.video_pipeline.batch_excel import HeyGenBatchRow
from services.video_pipeline.intake import (
    BatchSettings,
    IntakeError,
    SingleSubmission,
    spec_from_single,
    specs_from_batch,
)
from services.video_pipeline.types import VideoJobSpec

BASE = SingleSubmission(script="मेष का राशिफल", video_title="Aries")


def _spec(**changes) -> VideoJobSpec:
    return spec_from_single(replace(BASE, **changes))


@pytest.mark.parametrize(
    ("raw", "filed_as"),
    [
        ("Aries", "Aries"),
        ("  Aries  ", "Aries"),
        ("a/b\\c", "a_b_c"),
        ('a:*?"<>|b', "a_b"),          # a run of bad characters is one underscore
        ("Aries.mp4", "Aries"),
        ("Aries.MP4", "Aries"),
        ("Aries.mp4.mp4", "Aries.mp4"),  # only the trailing extension, as the pane does
        ("मेष 14-08", "मेष 14-08"),
    ],
)
def test_titles_are_scrubbed_like_the_pane_scrubs_them(raw, filed_as) -> None:
    assert _spec(video_title=raw).video_title == filed_as


@pytest.mark.parametrize("blank", ["", "   ", ".mp4"])
def test_a_title_that_scrubs_to_nothing_is_rejected(blank) -> None:
    # filed as ".mp4", or under a shared default that the next one overwrites
    with pytest.raises(IntakeError, match="video_title"):
        _spec(video_title=blank)


@pytest.mark.parametrize(
    ("script", "accepted"),
    [
        ("x" * MAX_SCRIPT_CHARS, True),
        ("x" * (MAX_SCRIPT_CHARS + 1), False),
        ("  " + "x" * MAX_SCRIPT_CHARS + "\n", True),  # measured as sent, trimmed
    ],
)
def test_the_script_is_capped_at_the_writers_limit(script, accepted) -> None:
    if accepted:
        assert _spec(script=script).script == script.strip()
    else:
        with pytest.raises(IntakeError, match=str(MAX_SCRIPT_CHARS)):
            _spec(script=script)


@pytest.mark.parametrize("blank", ["", "   \n"])
def test_an_empty_script_is_rejected(blank) -> None:
    with pytest.raises(IntakeError, match="script must not be empty"):
        _spec(script=blank)


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("character", None, "indian"),
        ("character", "", "indian"),
        ("character", "us", "us"),
        ("voice_id", "", None),
        ("voice_id", "v9", "v9"),
        ("motion_prompt", "", None),
        ("motion_prompt", "slow nod", "slow nod"),
        ("publish_date", "", None),
        ("publish_date", "2026-08-14", "2026-08-14"),
        ("talking_photo_id", "", None),
        ("talking_photo_id", "tp_1", "tp_1"),
    ],
)
def test_blanks_become_none_and_character_defaults(field, raw, expected) -> None:
    assert getattr(_spec(**{field: raw}), field) == expected


def test_numbers_pass_through() -> None:
    spec = _spec(width=720, height=1280, stability=0.4, similarity_boost=0.9, style=0.2)
    assert (spec.width, spec.height) == (720, 1280)
    assert (spec.stability, spec.similarity_boost, spec.style) == (0.4, 0.9, 0.2)


@pytest.mark.parametrize("field", ["include_overlay", "include_music", "use_speaker_boost"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True), ("", True),
        ("true", True), ("True", True), ("on", True), ("1", True), ("yes", True),
        ("false", False), ("FALSE", False), ("off", False), ("0", False), ("no", False),
    ],
)
def test_toggles_parse_like_a_checkbox(field, raw, expected) -> None:
    assert getattr(_spec(**{field: raw}), field) is expected


def test_overlay_and_music_are_independent() -> None:
    spec = _spec(include_overlay="false", include_music="true")
    assert (spec.include_overlay, spec.include_music) == (False, True)


def test_an_unreadable_toggle_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(IntakeError, match="include_music"):
        _spec(include_music="maybe")


# ── batch ─────────────────────────────────────────────────────────────────────

def _rows(*titles: str, script: str = "राशिफल") -> list[HeyGenBatchRow]:
    return [
        HeyGenBatchRow(row_index=i, script=script, video_title=t)
        for i, t in enumerate(titles, start=2)
    ]


def test_a_batch_becomes_one_spec_per_row_with_the_shared_settings() -> None:
    items = specs_from_batch(
        _rows("Aries", "Taurus"),
        BatchSettings(character="us", motion_prompt="slow nod", publish_date="14-08-2026"),
    )

    assert [i.row_index for i in items] == [2, 3]
    assert [i.spec.video_title for i in items] == ["Aries", "Taurus"]
    for item in items:
        assert item.spec.character == "us"
        assert item.spec.motion_prompt == "slow nod"
        assert item.spec.publish_date == "14-08-2026"


def test_batch_shared_blanks_take_the_same_defaults_as_a_single() -> None:
    (item,) = specs_from_batch(_rows("Aries"), BatchSettings(character="", motion_prompt="", publish_date=""))
    assert (item.spec.character, item.spec.motion_prompt, item.spec.publish_date) == ("indian", None, None)


@pytest.mark.parametrize(
    ("overlay", "music", "expected"),
    [
        (None, None, (True, True)),
        ("false", "true", (False, True)),
        ("true", "false", (True, False)),
        ("false", "false", (False, False)),
    ],
)
def test_batch_toggles_are_carried_to_every_row(overlay, music, expected) -> None:
    items = specs_from_batch(
        _rows("Aries", "Taurus", "Gemini"),
        BatchSettings(include_overlay=overlay, include_music=music),
    )
    assert {(i.spec.include_overlay, i.spec.include_music) for i in items} == {expected}


def test_batch_titles_are_scrubbed_per_row() -> None:
    items = specs_from_batch(_rows("a/b", "Leo.mp4"), BatchSettings())
    assert [i.spec.video_title for i in items] == ["a_b", "Leo"]


def test_duplicate_titles_are_rejected_by_name() -> None:
    with pytest.raises(IntakeError, match="Aries") as exc:
        specs_from_batch(_rows("Aries", "Taurus", "Aries"), BatchSettings())
    assert "Taurus" not in str(exc.value)


def test_titles_that_collide_only_after_scrubbing_are_duplicates() -> None:
    # Both are filed as a_b.mp4 — and the NAS share does not care about case.
    with pytest.raises(IntakeError, match="a_b"):
        specs_from_batch(_rows("a/b", "A:B.mp4"), BatchSettings())


def test_a_row_over_the_length_cap_is_rejected_naming_its_row() -> None:
    rows = _rows("Aries") + [
        HeyGenBatchRow(row_index=7, script="x" * (MAX_SCRIPT_CHARS + 1), video_title="Taurus")
    ]
    with pytest.raises(IntakeError, match=rf"row 7.*{MAX_SCRIPT_CHARS}"):
        specs_from_batch(rows, BatchSettings())


def test_an_unreadable_batch_toggle_is_rejected() -> None:
    with pytest.raises(IntakeError, match="include_overlay"):
        specs_from_batch(_rows("Aries"), BatchSettings(include_overlay="maybe"))
