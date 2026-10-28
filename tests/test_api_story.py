from fastapi.testclient import TestClient

from api import routes as api
from services.story import MAX_LINE_CHARS, STORY_EMOTIONS, StoryGeminiError, StoryLine, StorySplitError
from tests.test_api_batch import _configure


def _client() -> TestClient:
    client = TestClient(api.app)
    _configure(client)
    return client


def test_split_returns_the_activity_name_and_lines(monkeypatch) -> None:
    seen: dict = {}

    def fake_split(text, *, settings):
        seen["text"], seen["api_key"] = text, settings.api_key
        return [
            StoryLine(text="Hi there!", emotion="Cheerful"),
            StoryLine(text="Moti barked.", emotion="Narrative"),
        ]

    monkeypatch.setattr(api, "split_story", fake_split)

    response = _client().post("/batch/story/split", json={
        "title": "The boy who followed the stream",
        "text": "Hi there! Moti barked.",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["activity_name"] == "theboywhofollowedthestreamstory"
    assert body["lines"] == [
        {"text": "Hi there!", "emotion": "Cheerful"},
        {"text": "Moti barked.", "emotion": "Narrative"},
    ]
    assert body["emotions"] == STORY_EMOTIONS
    assert body["max_line_chars"] == MAX_LINE_CHARS
    # the session's Gemini key reached the split
    assert seen == {"text": "Hi there! Moti barked.", "api_key": "test-google"}


def test_split_refuses_empty_text(monkeypatch) -> None:
    def fake_split(text, *, settings):
        raise AssertionError("Gemini must not be called for an empty story")

    monkeypatch.setattr(api, "split_story", fake_split)

    response = _client().post("/batch/story/split", json={"title": "T", "text": "  \n "})

    assert response.status_code == 400
    assert "empty" in response.json()["detail"]


def test_split_maps_a_rejected_split_to_400(monkeypatch) -> None:
    def fake_split(text, *, settings):
        raise StorySplitError("This story is too long for one run: the limit is 120 lines.")

    monkeypatch.setattr(api, "split_story", fake_split)

    response = _client().post("/batch/story/split", json={"title": "T", "text": "Once upon a time."})

    assert response.status_code == 400
    assert "too long" in response.json()["detail"]


def test_split_maps_gemini_being_down_to_502(monkeypatch) -> None:
    def fake_split(text, *, settings):
        raise StoryGeminiError("Gemini story split failed after 3 models")

    monkeypatch.setattr(api, "split_story", fake_split)

    response = _client().post("/batch/story/split", json={"title": "T", "text": "Once upon a time."})

    assert response.status_code == 502
