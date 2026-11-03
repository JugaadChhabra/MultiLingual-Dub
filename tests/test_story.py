import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from services import story
from services.qc import QCSettings
from services.story import (
    MAX_LINE_CHARS,
    MAX_STORY_LINES,
    STORY_EMOTIONS,
    StoryLine,
    StorySplitError,
    split_story,
)

from tests.fakes import patch_openrouter

SHEET = Path(__file__).resolve().parent.parent / "the_boy_who_followed_the_stream_voiceoversheet.xlsx"


def _reference_lines() -> list[tuple[str, str]]:
    ws = load_workbook(SHEET).active
    rows = list(ws.iter_rows(values_only=True))[1:]
    return [(text, emotion) for text, emotion, *_ in rows]


REFERENCE = _reference_lines()
# The operator pastes plain text; the sheet's cells joined with spaces is that text.
STORY = " ".join(text for text, _ in REFERENCE)

SETTINGS = QCSettings(api_key="test-key", models=["model-a", "model-b"], enabled=True)


def _reply(lines: list[tuple[str, str]]) -> str:
    return json.dumps({"lines": [{"text": t, "emotion": e} for t, e in lines]})


class FakeModel:
    """Hands out one queued reply per call; records what was asked."""

    def __init__(self, monkeypatch, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict] = []
        fake = self

        class FakeResponse:
            def __init__(self, text: str):
                self.text = text

        class FakeModels:
            def generate_content(self, model: str, contents: str, config=None):
                fake.calls.append({
                    "model": model,
                    "contents": contents,
                    "system": getattr(config, "system_instruction", "") or "",
                    "schema": getattr(config, "response_schema", None),
                })
                return FakeResponse(fake.replies.pop(0))

        monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")
        monkeypatch.setattr("services.story.STORY_MODELS", ["model-a", "model-b"])
        patch_openrouter(monkeypatch, "services.story.OpenRouterClient", FakeModels)


def test_happy_split_returns_the_lines_and_emotions(monkeypatch) -> None:
    model = FakeModel(monkeypatch, [_reply(REFERENCE)])

    lines = split_story(STORY, settings=SETTINGS)

    assert lines == [StoryLine(text=t, emotion=e) for t, e in REFERENCE]
    assert len(model.calls) == 1
    # the story goes to the model, and the allowed emotions are spelled out for it
    assert "Moti wagged his tail and barked." in model.calls[0]["contents"]
    for emotion in STORY_EMOTIONS:
        assert emotion in model.calls[0]["system"]


def test_quote_style_and_whitespace_do_not_count_as_changed_wording(monkeypatch) -> None:
    # The model straightens the curly apostrophe in "Let’s" and the source has
    # stray line breaks; neither is a change to what gets voiced.
    source = "Hi there!   Let’s follow Kiran.\n\n“Moti, look,” said Kiran."
    FakeModel(monkeypatch, [_reply([
        ("Hi there! Let's follow Kiran.", "Cheerful"),
        ('"Moti, look," said Kiran.', "Narrative"),
    ])])

    lines = split_story(source, settings=SETTINGS)

    assert [line.emotion for line in lines] == ["Cheerful", "Narrative"]


def test_emotion_case_and_typography_are_not_held_against_the_split(monkeypatch) -> None:
    source = "Moti barked… and ran."
    FakeModel(monkeypatch, [_reply([("Moti barked... and ran.", "narrative")])])

    assert split_story(source, settings=SETTINGS) == [StoryLine("Moti barked... and ran.", "Narrative")]


def test_a_model_failure_falls_back_to_the_next_model(monkeypatch) -> None:
    fake = FakeModel(monkeypatch, [_reply(REFERENCE)])
    calls: list[str] = []

    class FlakyModels:
        def generate_content(self, model: str, contents: str, config=None):
            calls.append(model)
            if model == "model-a":
                raise RuntimeError("429 rate limit")
            return type("R", (), {"text": fake.replies.pop(0)})()

    patch_openrouter(monkeypatch, "services.story.OpenRouterClient", FlakyModels)

    assert len(split_story(STORY, settings=SETTINGS)) == len(REFERENCE)
    assert calls == ["model-a", "model-b"]


def test_changed_wording_is_retried_once_then_rejected(monkeypatch) -> None:
    reworded = list(REFERENCE)
    reworded[6] = ("Moti wagged his tail and barked happily.", "Narrative")
    model = FakeModel(monkeypatch, [_reply(reworded), _reply(reworded)])

    with pytest.raises(StorySplitError, match="wording"):
        split_story(STORY, settings=SETTINGS)

    assert len(model.calls) == 2


def test_a_bad_first_reply_is_recovered_by_the_retry(monkeypatch) -> None:
    dropped = REFERENCE[:-1]
    model = FakeModel(monkeypatch, [_reply(dropped), _reply(REFERENCE)])

    lines = split_story(STORY, settings=SETTINGS)

    assert len(lines) == len(REFERENCE)
    assert len(model.calls) == 2


def test_unknown_emotion_is_rejected(monkeypatch) -> None:
    bad = list(REFERENCE)
    bad[2] = (bad[2][0], "Melancholic")
    FakeModel(monkeypatch, [_reply(bad), _reply(bad)])

    with pytest.raises(StorySplitError, match="Melancholic"):
        split_story(STORY, settings=SETTINGS)


def test_a_line_over_the_cap_is_rejected(monkeypatch) -> None:
    # One unbroken run of words: joined it still matches the source, so only the
    # length rule can catch it.
    sentence = "The stream ran on and on past the trees. "
    source = (sentence * (MAX_LINE_CHARS // len(sentence) + 1)).strip()
    FakeModel(monkeypatch, [_reply([(source, "Narrative")])] * 2)

    with pytest.raises(StorySplitError, match=str(MAX_LINE_CHARS)):
        split_story(source, settings=SETTINGS)


def test_the_cap_counts_the_emotion_tag() -> None:
    text = "a" * (MAX_LINE_CHARS - len("[Narrative] "))
    assert story.line_problem(StoryLine(text=text, emotion="Narrative")) is None
    assert story.line_problem(StoryLine(text=text + "a", emotion="Narrative")) is not None


def test_a_story_split_into_too_many_lines_is_refused(monkeypatch) -> None:
    words = [f"Word{i}." for i in range(MAX_STORY_LINES + 1)]
    source = " ".join(words)
    model = FakeModel(monkeypatch, [_reply([(w, "Narrative") for w in words])] * 2)

    with pytest.raises(StorySplitError, match="too long"):
        split_story(source, settings=SETTINGS)

    # an over-split is a bad split like any other, so it gets the retry too
    assert len(model.calls) == 2


def test_a_story_too_big_to_fit_is_refused_without_calling_a_model(monkeypatch) -> None:
    model = FakeModel(monkeypatch, [])
    source = "word " * (MAX_STORY_LINES * MAX_LINE_CHARS // 5 + 1)

    with pytest.raises(StorySplitError, match="too long"):
        split_story(source, settings=SETTINGS)

    assert model.calls == []


def test_empty_text_is_refused(monkeypatch) -> None:
    model = FakeModel(monkeypatch, [])

    with pytest.raises(StorySplitError):
        split_story("   \n ", settings=SETTINGS)

    assert model.calls == []


def test_reply_that_is_not_json_is_retried_then_rejected(monkeypatch) -> None:
    FakeModel(monkeypatch, ["Sure! Here are your lines:", "```json\nnope\n```"])

    with pytest.raises(StorySplitError):
        split_story(STORY, settings=SETTINGS)


def test_reply_in_a_code_fence_is_accepted(monkeypatch) -> None:
    FakeModel(monkeypatch, ["```json\n" + _reply(REFERENCE) + "\n```"])

    assert len(split_story(STORY, settings=SETTINGS)) == len(REFERENCE)


def test_activity_name_from_title() -> None:
    assert story.activity_name("The boy who followed the stream") == "theboywhofollowedthestreamstory"
    assert story.activity_name("  Kiran's Map! (Part 2) ") == "kiransmappart2story"


# ── story fix ────────────────────────────────────────────────────────────────

FIX_LINES = {
    "1": StoryLine(text="Kiran called Moti.", emotion="Narrative"),
    "2": StoryLine(text="Moti barked.", emotion="Cheerful"),
}
FIX_DRAFTS = {"1": "किरण ने मोती को बुलाया।", "2": "मोती भौंका।"}


def test_fix_prompt_carries_the_hindi_register_and_every_line_id(monkeypatch) -> None:
    model = FakeModel(monkeypatch, [json.dumps({"1": "किरण ने मोती को आवाज़ दी।", "2": "मोती भौंकने लगा।"})])

    story.fix_story("The stream", FIX_LINES, FIX_DRAFTS, "hi-IN", settings=SETTINGS)

    [call] = model.calls
    assert "parent tells a bedtime story" in call["system"]
    assert "स्कूल" in call["system"] and "बस" in call["system"]
    # per-story data goes in contents, not the system prompt
    sent = json.loads(call["contents"][call["contents"].index("["):])
    assert [line["id"] for line in sent] == ["1", "2"]
    assert sent[1] == {"id": "2", "english": "Moti barked.", "emotion": "Cheerful", "draft": "मोती भौंका।"}
    assert "The stream" in call["contents"]
    # QC's "no unnecessary English" rule does not carry over: loanwords stay
    assert "unnecessary English" not in call["system"]
    # the reply is held to one string per line id, and nothing else
    assert call["schema"]["required"] == ["1", "2"]
    assert call["schema"]["additionalProperties"] is False


def test_fix_prompt_for_another_language_uses_everyday_register(monkeypatch) -> None:
    model = FakeModel(monkeypatch, [json.dumps({"1": "a", "2": "b"})])

    story.fix_story("T", FIX_LINES, FIX_DRAFTS, "ta-IN", settings=SETTINGS)

    system = model.calls[0]["system"]
    assert "Everyday spoken Tamil" in system
    assert "bedtime" not in system


def test_fix_reply_is_parsed_into_a_mapping_keyed_by_line_id(monkeypatch) -> None:
    reply = "```json\n" + json.dumps({"1": " किरण ने आवाज़ दी। ", "2": "मोती भौंकने लगा।", "9": "stray"}) + "\n```"
    FakeModel(monkeypatch, [reply])

    fixed = story.fix_story("T", FIX_LINES, FIX_DRAFTS, "hi-IN", settings=SETTINGS)

    # trimmed, and an id that was never asked for is dropped
    assert fixed == {"1": "किरण ने आवाज़ दी।", "2": "मोती भौंकने लगा।"}


def test_fix_reply_missing_or_empty_ids_leaves_them_out(monkeypatch) -> None:
    FakeModel(monkeypatch, [json.dumps({"1": "  "})])

    assert story.fix_story("T", FIX_LINES, FIX_DRAFTS, "hi-IN", settings=SETTINGS) == {}


def test_fix_that_no_model_can_answer_raises(monkeypatch) -> None:
    FakeModel(monkeypatch, ["not json", "still not json"])

    with pytest.raises(story.StoryFixError):
        story.fix_story("T", FIX_LINES, FIX_DRAFTS, "hi-IN", settings=SETTINGS)
