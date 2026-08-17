"""Tests for the script writer — the thing that replaced the daily spreadsheet.

The behaviours worth pinning: an operator gets twelve signs or an error (never
eleven), a draft is always short enough to actually render, and what was written
on earlier days reaches the prompt.
"""
from __future__ import annotations

import json

import pytest

from services import script_writer
from services.script_writer import (
    MAX_SCRIPT_CHARS,
    TARGET_CHARS_HIGH,
    TARGET_CHARS_LOW,
    ZODIAC_SIGNS,
    DraftScript,
    ScriptWriterError,
    ScriptWriterSettings,
    SetItem,
    fit_to_limit,
    write_daily_scripts,
)

# Titles are the Devanagari names the hand-authored reference sheet files under.
HINDI_TITLES = ["मेष", "वृषभ", "मिथुन", "कर्क", "सिंह", "कन्या",
                "तुला", "वृश्चिक", "धनु", "मकर", "कुंभ", "मीन"]


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def _install(monkeypatch, responses: dict[str, str] | str, captured: dict | None = None):
    """Stub Gemini. ``responses`` is either one body for every model, or a body
    per model name — a value that is an Exception is raised instead."""

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            if captured is not None:
                captured["model"] = model
                captured["contents"] = contents
                captured["system"] = getattr(config, "system_instruction", "") or ""
            body = responses if isinstance(responses, str) else responses[model]
            if isinstance(body, Exception):
                raise body
            return FakeResponse(body)

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            self.models = FakeModels()

    monkeypatch.setattr("services.script_writer.genai.Client", FakeClient)


def _settings(models: list[str] | None = None) -> ScriptWriterSettings:
    return ScriptWriterSettings(api_key="key", models=models or ["model-a"])


def _zodiac_body(text: str = "आज का दिन अच्छा है।") -> str:
    # Keyed by the Latin key the model answers under, not by the file title.
    return json.dumps({s.key: f"{s.key}: {text}" for s in ZODIAC_SIGNS}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def no_retry_sleeping(monkeypatch):
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")


def test_writes_one_draft_per_sign_titled_by_sign(monkeypatch) -> None:
    _install(monkeypatch, _zodiac_body())

    drafts = write_daily_scripts(
        brief="Daily horoscope",
        language="hi-IN",
        publish_date="2026-08-17",
        settings=_settings(),
    )

    assert [d.title for d in drafts] == HINDI_TITLES
    assert all(d.script for d in drafts)


def test_a_missing_sign_fails_the_run_rather_than_shortening_it(monkeypatch) -> None:
    partial = json.loads(_zodiac_body())
    del partial["Pisces"]  # the key, whose title is मीन
    _install(monkeypatch, json.dumps(partial, ensure_ascii=False))

    with pytest.raises(ScriptWriterError, match="Pisces"):
        write_daily_scripts(
            brief="Daily horoscope",
            language="hi-IN",
            publish_date="2026-08-17",
            settings=_settings(),
        )


def test_falls_through_to_the_next_model(monkeypatch) -> None:
    captured: dict = {}
    _install(
        monkeypatch,
        {"model-a": RuntimeError("429 rate limit"), "model-b": _zodiac_body()},
        captured,
    )

    drafts = write_daily_scripts(
        brief="Daily horoscope",
        language="hi-IN",
        publish_date="2026-08-17",
        settings=_settings(["model-a", "model-b"]),
    )

    assert len(drafts) == len(ZODIAC_SIGNS)
    assert captured["model"] == "model-b"


def test_a_fenced_response_is_still_read(monkeypatch) -> None:
    _install(monkeypatch, "```json\n" + _zodiac_body() + "\n```")

    drafts = write_daily_scripts(
        brief="Daily horoscope",
        language="hi-IN",
        publish_date="2026-08-17",
        settings=_settings(),
    )

    assert len(drafts) == len(ZODIAC_SIGNS)


def test_an_overlong_script_is_trimmed_to_a_renderable_length(monkeypatch) -> None:
    long_script = ("यह एक बहुत लंबा वाक्य है। " * 200).strip()
    _install(monkeypatch, json.dumps({s.key: long_script for s in ZODIAC_SIGNS}, ensure_ascii=False))

    drafts = write_daily_scripts(
        brief="Daily horoscope",
        language="hi-IN",
        publish_date="2026-08-17",
        settings=_settings(),
    )

    assert all(len(d.script) <= MAX_SCRIPT_CHARS for d in drafts)
    assert all(d.script.endswith("।") for d in drafts)


def test_fit_to_limit_leaves_a_short_script_alone() -> None:
    assert fit_to_limit("Short enough.") == "Short enough."


def test_fit_to_limit_cuts_at_a_sentence_end() -> None:
    text = "One sentence here. Two sentence here. Three."
    assert fit_to_limit(text, limit=40) == "One sentence here. Two sentence here."


def test_the_date_the_brief_and_recent_scripts_all_reach_the_prompt(monkeypatch) -> None:
    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)

    write_daily_scripts(
        brief="Calm devotional horoscope",
        language="hi-IN",
        publish_date="2026-08-17",
        recent=[DraftScript(title="2026-08-16 मेष", script="कल का मेष राशिफल")],
        settings=_settings(),
    )

    assert "2026-08-17" in captured["contents"]
    assert "कल का मेष राशिफल" in captured["contents"]
    assert "Calm devotional horoscope" in captured["system"]
    # Language is named in the system instruction, not left to the model to
    # infer from the brief.
    assert "Hindi" in captured["system"]


def test_the_prompt_asks_for_the_tagged_three_paragraph_shape(monkeypatch) -> None:
    """The reference sheet's scripts are three audio-tagged paragraphs, and the
    speech model only performs the tags if they are bracketed and unspoken."""
    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)

    write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                        publish_date="2026-08-17", settings=_settings())

    system = captured["system"]
    assert "[शांत, आत्मविश्वासी आवाज़]" in system and "[सामान्य गति]" in system
    assert "never spoken" in system
    # Both the key the model answers under and the title it must speak.
    assert "Aries" in system and "मेष" in system
    # Length is stated in both messages — in the system instruction alone the
    # model returned half of it.
    assert str(TARGET_CHARS_LOW) in system and str(TARGET_CHARS_LOW) in captured["contents"]
    assert str(TARGET_CHARS_HIGH) in captured["contents"]


def test_a_single_item_set_writes_one_script(monkeypatch) -> None:
    _install(monkeypatch, json.dumps({"diwali_promo": "एक स्क्रिप्ट।"}, ensure_ascii=False))

    drafts = write_daily_scripts(
        brief="One video",
        language="hi-IN",
        publish_date="2026-08-17",
        items=(SetItem("diwali_promo", "diwali_promo"),),
        settings=_settings(),
    )

    assert drafts == [DraftScript(title="diwali_promo", script="एक स्क्रिप्ट।")]


def test_an_empty_brief_is_refused_before_any_call(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("must not reach Gemini")

    monkeypatch.setattr("services.script_writer.genai.Client", explode)

    with pytest.raises(ScriptWriterError, match="brief"):
        write_daily_scripts(
            brief="   ",
            language="hi-IN",
            publish_date="2026-08-17",
            settings=_settings(),
        )
