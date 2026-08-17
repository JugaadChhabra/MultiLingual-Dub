"""Route tests for generated scripts: the draft endpoint, and the batch path
that takes reviewed rows instead of a spreadsheet.

The pair matters as a pair — generation returns text and nothing else, and the
render only starts on a second request carrying what the operator approved. A
regression that let generation start renders would burn twelve paid renders per
day unattended.
"""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from api import routes as api
from services.script_writer import ZODIAC_SIGNS, DraftScript, ScriptWriterError

ENV_TEXT = "\n".join(
    [
        "ELEVEN_LABS=test-eleven",
        "SARVAM_API=test-sarvam",
        "GEMINI_API_KEY=test-google",
        "HEYGEN_ISHWARI=test-heygen",
        "AWS_ACCESS_KEY=abc",
        "AWS_SECRET_KEY=xyz",
        "AWS_BUCKET=test-bucket",
        "AWS_REGION=ap-south-1",
        "BATCH_ENABLE_S3_UPLOAD=true",
        "BATCH_ENABLE_QC=true",
        "DESI_VOCAL_VOICE=v2",
        "ENGLISH_VOICE=v3",
        "NAS_MODE=local",
        "NAS_ROOT_PATH=/tmp/nas-test",
        "RESEND_API_KEY=re_test",
        "RESEND_FROM_ADDRESS=a@b.c",
        "NOTIFY_EMAILS=a@b.c",
    ]
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # History is per-deployment state on disk; point it at a temp dir so a test
    # run neither reads nor writes the developer's real output/ folder.
    monkeypatch.setattr(api, "script_history", type(api.script_history)(tmp_path / "scripts"))
    client = TestClient(api.app)
    resp = client.post("/config/session-env", json={"env_text": ENV_TEXT})
    assert resp.status_code == 200, resp.text
    return client


def _stub_writer(monkeypatch, captured: dict | None = None):
    def fake(*, brief, language, publish_date, items, recent, settings):
        if captured is not None:
            captured.update(
                brief=brief, language=language, publish_date=publish_date,
                items=items, recent=recent,
            )
        return [DraftScript(title=item.title, script=f"{item.key} script") for item in items]

    monkeypatch.setattr(api, "write_daily_scripts", fake)


def test_generate_returns_twelve_drafts_and_starts_nothing(client, monkeypatch) -> None:
    _stub_writer(monkeypatch)

    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17",
              "language": "hi-IN", "category": "horoscope"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Files are named the way the hand-authored sheet names them: Devanagari.
    assert [item["video_title"] for item in body["items"]] == [s.title for s in ZODIAC_SIGNS]
    assert body["items"][0]["video_title"] == "मेष"
    assert all(item["script"] for item in body["items"])


def test_generation_feeds_earlier_days_back_to_the_writer(client, monkeypatch) -> None:
    captured: dict = {}
    _stub_writer(monkeypatch, captured)

    client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-16",
              "language": "hi-IN", "category": "horoscope"},
    )
    assert captured["recent"] == []

    client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17",
              "language": "hi-IN", "category": "horoscope"},
    )
    assert [d.title for d in captured["recent"]][:1] == ["2026-08-16 मेष"]


def test_a_single_item_set_uses_the_operators_title(client, monkeypatch) -> None:
    captured: dict = {}
    _stub_writer(monkeypatch, captured)

    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "One video", "publish_date": "2026-08-17",
              "item_set": "single", "title": "diwali_promo"},
    )

    assert resp.status_code == 200
    assert [(i.key, i.title) for i in captured["items"]] == [("diwali_promo", "diwali_promo")]


def test_a_category_with_no_brief_is_refused(client) -> None:
    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "  ", "publish_date": "2026-08-17"},
    )
    assert resp.status_code == 400
    assert "script brief" in resp.json()["detail"]


def test_a_failed_generation_surfaces_as_a_bad_gateway(client, monkeypatch) -> None:
    def fake(**_kwargs):
        raise ScriptWriterError("Gemini said no")

    monkeypatch.setattr(api, "write_daily_scripts", fake)

    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17"},
    )
    assert resp.status_code == 502
    assert "Gemini said no" in resp.json()["detail"]


# --- reviewed rows into the batch -----------------------------------------


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["script", "video_title"])
    ws.append(["hello", "Aries"])
    buff = BytesIO()
    wb.save(buff)
    return buff.getvalue()


def _image():
    return {"image": ("avatar.jpg", b"jpeg-bytes", "image/jpeg")}


def test_batch_accepts_reviewed_rows_instead_of_a_spreadsheet(client, monkeypatch) -> None:
    started: dict = {}

    async def fake_batch(**kwargs):
        started["rows"] = kwargs["rows"]

    monkeypatch.setattr(api, "run_video_batch_job", fake_batch)

    rows = [{"script": "मेष का राशिफल", "video_title": "Aries"},
            {"script": "वृषभ का राशिफल", "video_title": "Taurus"}]
    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps(rows), "character": "indian"},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["total"] == 2
    state = client.get(f"/video/heygen/batch/{resp.json()['batch_id']}").json()
    assert [row["video_title"] for row in state["rows"]] == ["Aries", "Taurus"]


def test_the_rows_submitted_replace_the_drafts_in_the_history(client, monkeypatch) -> None:
    """What viewers hear is the edited script, so that is what later days must be
    told not to repeat — not the draft the model first produced."""
    _stub_writer(monkeypatch)

    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(api, "run_video_batch_job", fake_batch)

    client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17",
              "language": "hi-IN", "category": "sunsign"},
    )
    assert [d.script for d in api.script_history.recent(
        category="sunsign", language="hi-IN")] == [f"{s.key} script" for s in ZODIAC_SIGNS]

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "मैंने इसे बदल दिया", "video_title": "मेष"}]),
              "publish_date": "2026-08-17", "category": "sunsign", "language": "hi-IN"},
    )
    assert resp.status_code == 202, resp.text

    recorded = api.script_history.recent(category="sunsign", language="hi-IN")
    assert [(d.title, d.script) for d in recorded] == [("2026-08-17 मेष", "मैंने इसे बदल दिया")]


def test_a_spreadsheet_batch_records_no_history(client, monkeypatch) -> None:
    """An Excel batch carries no category or language to file it under, and its
    scripts were not written by the model that would be told to avoid them."""
    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(api, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files={
            **_image(),
            "excel": ("in.xlsx", _xlsx_bytes(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        },
        data={"publish_date": "2026-08-17"},
    )

    assert resp.status_code == 202, resp.text
    assert api.script_history.recent(category="sunsign", language="hi-IN") == []


def test_rows_without_a_history_key_still_render(client, monkeypatch) -> None:
    """Recording is a side effect of a run, never a precondition for one."""
    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(api, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "कोई स्क्रिप्ट", "video_title": "मेष"}])},
    )
    assert resp.status_code == 202, resp.text


def test_batch_rejects_a_request_carrying_both_rows_and_a_spreadsheet(client) -> None:
    resp = client.post(
        "/video/heygen/batch",
        files={
            **_image(),
            "excel": ("in.xlsx", _xlsx_bytes(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        },
        data={"rows": json.dumps([{"script": "s", "video_title": "t"}])},
    )
    assert resp.status_code == 400


def test_batch_rejects_a_request_carrying_neither(client) -> None:
    resp = client.post("/video/heygen/batch", files=_image(), data={})
    assert resp.status_code == 400


def test_batch_rejects_rows_that_are_not_json(client) -> None:
    resp = client.post("/video/heygen/batch", files=_image(), data={"rows": "not json"})
    assert resp.status_code == 400
    assert "valid JSON" in resp.json()["detail"]


def test_batch_rejects_rows_with_no_scripts_left_in_them(client) -> None:
    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "   ", "video_title": "Aries"}])},
    )
    assert resp.status_code == 400
