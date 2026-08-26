"""Tests for the script writer — the thing that replaced the daily spreadsheet.

The behaviours worth pinning: an operator gets twelve signs or an error (never
eleven), a draft is always short enough to actually render, and what was written
on earlier days reaches the prompt.
"""
from __future__ import annotations

import json

import pytest

from services import script_writer
from services.script_validate import ALL_TAGS, HistoryFacts
from services.script_writer import (
    MAX_SCRIPT_CHARS,
    ACCEPT_CHARS_HIGH,
    ACCEPT_CHARS_LOW,
    ASK_CHARS_HIGH,
    ASK_CHARS_LOW,
    ZODIAC_SIGNS,
    DraftScript,
    ScriptRepairError,
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


# Twelve colours no two of which contain each other, so the same-day collision
# rule is not tripped by the fixtures themselves.
COLOURS = ["नीला", "हरा", "पीला", "गुलाबी", "बैंगनी", "नारंगी",
           "भूरा", "सफेद", "काला", "लाल", "फिरोजी", "मैरून"]
DEVANAGARI_DIGITS = "०१२३४५६७८९"


def _digits(number: int) -> str:
    return "".join(DEVANAGARI_DIGITS[int(d)] for d in str(number))


def valid_script(*, colour: str, number: int, areas: str = "नई शुरुआत, ऊर्जा और नेतृत्व",
                 tag: str = "warm") -> str:
    """A script the validator accepts: tagged in English, with a double-digit number.

    Deliberately shorter than the real length target — length is a soft rule, so
    a fixture does not have to be 400 characters to be valid, and a fixture that
    was would bury what each test is actually about.
    """
    return (
        f"[{tag}] मेष राशि के जातकों... आज {areas}!\n\n"
        "[reassuring] एक अवसर मिलेगा। [optimistic] दिन अच्छा बीतेगा। "
        "[calm] स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी। "
        f"[bright] शुभ रंग: {colour} | जादुई अंक: संख्या ({_digits(number)})।\n\n"
        "[uplifting] आज का दिन शुभ है।"
    )


def _zodiac_body() -> str:
    """One valid script per sign, each with its own colour and number."""
    # Keyed by the Latin key the model answers under, not by the file title.
    return json.dumps(
        {
            sign.key: valid_script(colour=COLOURS[i], number=10 + i)
            for i, sign in enumerate(ZODIAC_SIGNS)
        },
        ensure_ascii=False,
    )


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


def test_an_overlong_script_is_refused_rather_than_silently_decapitated(monkeypatch) -> None:
    """Trimming an overlong script throws away its closing line.

    That used to happen silently. A script that has lost its ending is a broken
    video, so it is now a rewrite request, and a model that will not write
    shorter fails the run instead of shipping the stump.
    """
    long_script = ("यह एक बहुत लंबा वाक्य है। " * 200).strip()
    _install(monkeypatch, json.dumps({s.key: long_script for s in ZODIAC_SIGNS}, ensure_ascii=False))

    with pytest.raises(ScriptWriterError, match="repair"):
        write_daily_scripts(
            brief="Daily horoscope",
            language="hi-IN",
            publish_date="2026-08-17",
            settings=_settings(),
        )


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


def test_the_prompt_asks_for_english_delivery_tags(monkeypatch) -> None:
    """ElevenLabs v3 reads delivery direction only as English bracketed words.

    The Devanagari tags this prompt used to mandate were handed to the voice
    engine, which has no idea what they mean — so the prompt now names the
    English bank and forbids the old ones by example.
    """
    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)

    write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                        publish_date="2026-08-17", settings=_settings())

    system = captured["system"]
    for tag in ("[warm]", "[reassuring]", "[calm]", "[uplifting]"):
        assert tag in system
    assert "[सामान्य गति]" in system, "the old tag is named only as a counter-example"
    assert "Never write [सामान्य गति]" in system
    assert "never spoken" in system
    # Both the key the model answers under and the title it must speak.
    assert "Aries" in system and "मेष" in system
    # Length is stated in both messages — in the system instruction alone the
    # model returned half of it.
    assert str(ASK_CHARS_LOW) in system and str(ASK_CHARS_LOW) in captured["contents"]
    assert str(ASK_CHARS_HIGH) in captured["contents"]


def test_the_tag_bank_in_the_prompt_is_the_one_the_validator_checks(monkeypatch) -> None:
    """Prompt and check read the same dict, so they cannot drift apart."""
    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)

    write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                        publish_date="2026-08-17", settings=_settings())

    for tag in ALL_TAGS:
        assert f"[{tag}]" in captured["system"]


def test_a_single_item_set_writes_one_script(monkeypatch) -> None:
    """A one-off video has no colour or lucky number, and must not need one."""
    script = valid_script(colour="नीला", number=17).replace(
        " [bright] शुभ रंग: नीला | जादुई अंक: संख्या (१७)।", ""
    )
    _install(monkeypatch, json.dumps({"diwali_promo": script}, ensure_ascii=False))

    drafts = write_daily_scripts(
        brief="One video",
        language="hi-IN",
        publish_date="2026-08-17",
        items=(SetItem("diwali_promo", "diwali_promo"),),
        settings=_settings(),
    )

    assert drafts == [
        DraftScript(title="diwali_promo", script=script, key="diwali_promo")
    ]


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


def test_only_the_offending_script_is_rewritten(monkeypatch) -> None:
    """A repair must not perturb the scripts that were already good."""
    bad = json.loads(_zodiac_body())
    bad["Taurus"] = valid_script(colour=COLOURS[0], number=10)  # मेष's colour and number
    fixed = valid_script(colour="जामुनी", number=44)

    calls: list[dict] = []

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            calls.append({"contents": contents})
            if len(calls) == 1:
                return FakeResponse(json.dumps(bad, ensure_ascii=False))
            return FakeResponse(json.dumps({"Taurus": fixed}, ensure_ascii=False))

    monkeypatch.setattr(
        "services.script_writer.genai.Client",
        lambda api_key: type("C", (), {"models": FakeModels()})(),
    )

    drafts = write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                                 publish_date="2026-08-17", settings=_settings())

    by_key = {d.key: d.script for d in drafts}
    assert by_key["Taurus"] == fixed
    # Every other sign is byte-identical to the first response.
    for key, script in bad.items():
        if key != "Taurus":
            assert by_key[key] == script
    # The repair named only the failing sign.
    assert "Taurus" in calls[1]["contents"] and "Gemini" not in calls[1]["contents"]
    assert calls[1]["contents"].count("Sagittarius") == calls[0]["contents"].count("Sagittarius")


def test_a_set_that_keeps_repeating_is_refused(monkeypatch) -> None:
    clashing = json.loads(_zodiac_body())
    clashing["Taurus"] = valid_script(colour=COLOURS[0], number=10)
    _install(monkeypatch, json.dumps(clashing, ensure_ascii=False))

    with pytest.raises(ScriptWriterError, match="repair attempt"):
        write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                            publish_date="2026-08-17", settings=_settings())


def test_a_soft_violation_alone_does_not_trigger_a_repair(monkeypatch) -> None:
    """A reused area combination is duller, not wrong — it still ships."""
    calls: list[str] = []

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            calls.append(contents)
            return FakeResponse(_zodiac_body())

    monkeypatch.setattr(
        "services.script_writer.genai.Client",
        lambda api_key: type("C", (), {"models": FakeModels()})(),
    )

    drafts = write_daily_scripts(
        brief="Daily horoscope", language="hi-IN", publish_date="2026-08-17",
        history={"Aries": HistoryFacts(
            combinations=(frozenset({"नई शुरुआत", "ऊर्जा", "नेतृत्व"}),)
        )},
        settings=_settings(),
    )

    assert len(calls) == 1, "a soft violation must not cost a repair call"
    assert len(drafts) == len(ZODIAC_SIGNS)


def test_recent_colours_and_numbers_reach_the_prompt(monkeypatch) -> None:
    """Prose excerpts truncate before the fortune line, so facts are listed."""
    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)

    write_daily_scripts(
        brief="Daily horoscope", language="hi-IN", publish_date="2026-08-17",
        history={"Aries": HistoryFacts(colours=("रूबी रेड",), numbers=(42,))},
        settings=_settings(),
    )

    assert "रूबी रेड" in captured["contents"]
    assert "42" in captured["contents"]


def test_a_repeat_of_a_recent_colour_is_repaired(monkeypatch) -> None:
    body = _zodiac_body()
    fixed = valid_script(colour="जामुनी", number=44)
    calls: list[str] = []

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            calls.append(contents)
            if len(calls) == 1:
                return FakeResponse(body)
            return FakeResponse(json.dumps({"Aries": fixed}, ensure_ascii=False))

    monkeypatch.setattr(
        "services.script_writer.genai.Client",
        lambda api_key: type("C", (), {"models": FakeModels()})(),
    )

    drafts = write_daily_scripts(
        brief="Daily horoscope", language="hi-IN", publish_date="2026-08-17",
        # A shade of the colour मेष was given by _zodiac_body.
        history={"Aries": HistoryFacts(colours=(f"गहरा {COLOURS[0]}",))},
        settings=_settings(),
    )

    assert len(calls) == 2
    assert {d.key: d.script for d in drafts}["Aries"] == fixed


def test_a_partial_response_still_falls_through_to_the_next_model(monkeypatch) -> None:
    """A model that drops a sign is swapped out, not surfaced as an error.

    The repair loop needed its own exception to escape the fallthrough handler;
    catching the base error instead turned every routine partial response into
    a failed run.
    """
    partial = json.loads(_zodiac_body())
    del partial["Pisces"]
    captured: dict = {}
    _install(
        monkeypatch,
        {"model-a": json.dumps(partial, ensure_ascii=False), "model-b": _zodiac_body()},
        captured,
    )

    drafts = write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                                 publish_date="2026-08-17",
                                 settings=_settings(["model-a", "model-b"]))

    assert len(drafts) == len(ZODIAC_SIGNS)
    assert captured["model"] == "model-b"


def test_a_spent_repair_budget_does_not_retry_the_whole_thing_on_the_next_model(
    monkeypatch,
) -> None:
    """A collision the model would not fix is not worth another full budget."""
    clashing = json.loads(_zodiac_body())
    clashing["Taurus"] = valid_script(colour=COLOURS[0], number=10)
    calls: list[str] = []

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            calls.append(model)
            return FakeResponse(json.dumps(clashing, ensure_ascii=False))

    monkeypatch.setattr(
        "services.script_writer.genai.Client",
        lambda api_key: type("C", (), {"models": FakeModels()})(),
    )

    with pytest.raises(ScriptRepairError):
        write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                            publish_date="2026-08-17",
                            settings=_settings(["model-a", "model-b"]))

    assert set(calls) == {"model-a"}, "the fallback model must not be tried"


def test_yesterdays_tags_reach_the_prompt(monkeypatch) -> None:
    """A soft rule checks for this sequence repeating, so it must be shown."""
    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)

    write_daily_scripts(
        brief="Daily horoscope", language="hi-IN", publish_date="2026-08-17",
        history={"Aries": HistoryFacts(previous_tags=("warm", "measured", "sincere"))},
        settings=_settings(),
    )

    assert "[measured]" in captured["contents"] and "[sincere]" in captured["contents"]


def test_a_repair_is_told_which_colours_the_untouched_scripts_hold(monkeypatch) -> None:
    """Otherwise the rewrite collides with a script that was already clean.

    A same-day collision is blamed on the later of the pair, so the next round
    rejects a sign that started out fine — and the budget walks down the zodiac
    over collisions the model was never warned about.
    """
    bad = json.loads(_zodiac_body())
    bad["Taurus"] = valid_script(colour=COLOURS[0], number=10)
    calls: list[str] = []

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            calls.append(contents)
            if len(calls) == 1:
                return FakeResponse(json.dumps(bad, ensure_ascii=False))
            return FakeResponse(json.dumps(
                {"Taurus": valid_script(colour="जामुनी", number=44)}, ensure_ascii=False))

    monkeypatch.setattr(
        "services.script_writer.genai.Client",
        lambda api_key: type("C", (), {"models": FakeModels()})(),
    )

    write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                        publish_date="2026-08-17", settings=_settings())

    repair = calls[1]
    assert "Already in use today" in repair
    # A colour and a number held by a sign that is NOT being rewritten.
    assert COLOURS[5] in repair and "20" in repair
    # ...and not the failing sign's own, which it is being told to replace.
    assert "Taurus" in repair


def test_the_prompt_asks_for_less_than_the_check_accepts(monkeypatch) -> None:
    """Every spoken character is paid for twice — to synthesise and as runtime.

    So the ask is the shortest length that still carries all six beats, while
    acceptance is merely what is tolerable. Chasing the model's overshoot by
    widening acceptance is a treadmill; the ask is what gets tuned.
    """
    assert ASK_CHARS_HIGH < ACCEPT_CHARS_HIGH
    assert ACCEPT_CHARS_LOW < ASK_CHARS_LOW

    captured: dict = {}
    _install(monkeypatch, _zodiac_body(), captured)
    write_daily_scripts(brief="Daily horoscope", language="hi-IN",
                        publish_date="2026-08-17", settings=_settings())

    # The model is told the ask, never the tolerance — quoting the wider band
    # would just make it write to the wider band.
    for text in (captured["system"], captured["contents"]):
        assert str(ACCEPT_CHARS_HIGH) not in text
