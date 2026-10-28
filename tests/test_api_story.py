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


def _xlsx(client, payload):
    return client.post("/batch/story/xlsx", json=payload)


def test_xlsx_round_trips_through_the_excel_reader(tmp_path) -> None:
    from batch.excel import read_excel_rows

    response = _xlsx(_client(), {
        "activity_name": "moti1story",
        "lines": [
            {"text": "Hi there!", "emotion": "Cheerful"},
            {"text": "Moti barked.", "emotion": "Narrative"},
        ],
    })

    assert response.status_code == 200
    assert 'filename="moti1story.xlsx"' in response.headers["content-disposition"]
    path = tmp_path / "story.xlsx"
    path.write_bytes(response.content)
    rows = read_excel_rows(path)
    assert [r.text for r in rows] == ["Hi there!", "Moti barked."]
    assert [r.emotion for r in rows] == ["Cheerful", "Narrative"]
    assert {r.activity_name for r in rows} == {"moti1story"}
    assert [r.audio_type for r in rows] == ["moti1story1", "moti1story2"]


def test_xlsx_rejects_bad_input() -> None:
    client = _client()
    ok = {"text": "Hi.", "emotion": "Cheerful"}
    assert _xlsx(client, {"activity_name": "a", "lines": []}).status_code == 400
    assert _xlsx(client, {"activity_name": "a", "lines": [ok, {"text": "  ", "emotion": "Cheerful"}]}).status_code == 400
    assert _xlsx(client, {"activity_name": "a", "lines": [{"text": "Hi.", "emotion": "Furious"}]}).status_code == 400


# ── story job creation ───────────────────────────────────────────────────────

from tests.test_api_batch import ENV_TEXT, _poll_until_done  # noqa: E402


async def _fake_story_runner(
    *, job_id, title, activity_name, lines, target_languages, jobs_store, settings, output_dir=None,
):
    """Completes at once, recording what it was given."""
    _fake_story_runner.seen = {"title": title, "activity_name": activity_name,
                               "lines": lines, "target_languages": target_languages}
    await jobs_store.start(job_id)
    summary = (await jobs_store.get(job_id)).summary
    summary.total_rows = len(lines)
    summary.rows_processed = len(lines)
    await jobs_store.complete(job_id, summary)


def _job(client, **overrides):
    payload = {
        "title": "The stream",
        "activity_name": "thestreamstory",
        "lines": [{"text": "Hi there!", "emotion": "Cheerful"}, {"text": "Moti barked.", "emotion": "Narrative"}],
        "target_languages": ["hi-IN"],
    }
    payload.update(overrides)
    return client.post("/batch/story-jobs", json=payload)


def test_story_job_is_created_and_pollable(monkeypatch) -> None:
    monkeypatch.setattr(api, "run_story_job", _fake_story_runner)
    client = _client()

    response = _job(client, target_languages=["hi-IN", "ta-IN", "hi-IN"])

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    state = _poll_until_done(client, body["job_id"])
    assert state["status"] == "completed"
    assert state["summary"]["total_rows"] == 2
    seen = _fake_story_runner.seen
    assert seen["activity_name"] == "thestreamstory"
    assert seen["title"] == "The stream"
    assert seen["target_languages"] == ["hi-IN", "ta-IN"]
    assert [(line.text, line.emotion) for line in seen["lines"]] == [
        ("Hi there!", "Cheerful"), ("Moti barked.", "Narrative"),
    ]


def _refused(monkeypatch, client, **overrides):
    async def runner(**_kwargs):
        raise AssertionError("a refused story job must not start")

    monkeypatch.setattr(api, "run_story_job", runner)
    response = _job(client, **overrides)
    assert response.status_code == 400, response.text
    return response.json()["detail"]


def test_story_job_refuses_empty_lines(monkeypatch) -> None:
    client = _client()
    assert "no lines" in _refused(monkeypatch, client, lines=[])
    assert "Line 2" in _refused(monkeypatch, client, lines=[
        {"text": "Hi", "emotion": "Cheerful"}, {"text": "  ", "emotion": "Cheerful"},
    ])


def test_story_job_refuses_an_unknown_emotion(monkeypatch) -> None:
    detail = _refused(monkeypatch, _client(), lines=[{"text": "Hi", "emotion": "Furious"}])
    assert "Line 1" in detail and "Furious" in detail


def test_story_job_refuses_an_over_length_line(monkeypatch) -> None:
    detail = _refused(monkeypatch, _client(), lines=[{"text": "a" * MAX_LINE_CHARS, "emotion": "Calm"}])
    assert str(MAX_LINE_CHARS) in detail


def test_story_job_refuses_an_empty_activity_name(monkeypatch) -> None:
    assert "activity" in _refused(monkeypatch, _client(), activity_name="  ")


def test_story_job_refuses_no_target_languages(monkeypatch) -> None:
    assert "language" in _refused(monkeypatch, _client(), target_languages=[])


def test_story_job_needs_an_english_voice_for_english(monkeypatch) -> None:
    monkeypatch.delenv("ENGLISH_VOICE", raising=False)
    client = TestClient(api.app)
    env = "\n".join(line for line in ENV_TEXT.splitlines() if not line.startswith("ENGLISH_VOICE"))
    assert client.post("/config/session-env", json={"env_text": env}).status_code == 200

    assert "ENGLISH_VOICE" in _refused(monkeypatch, client, target_languages=["hi-IN", "en-IN"])


def test_story_job_is_cancelled_through_the_batch_endpoint(monkeypatch) -> None:
    async def never_finishes(*, job_id, jobs_store, **_kwargs):
        await jobs_store.start(job_id)

    monkeypatch.setattr(api, "run_story_job", never_finishes)
    client = _client()
    job_id = _job(client).json()["job_id"]

    response = client.post(f"/batch/excel-jobs/{job_id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelling"
