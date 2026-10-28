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
from api import shared
from api.routers import video_batch as video_batch_routes
from services.script_history import DraftScript, ScriptHistoryStore
from services.script_validate import Violation
from services.script_writer import (
    MAX_SCRIPT_CHARS,
    ZODIAC_SIGNS,
    ScriptRepairError,
    ScriptWriter,
    ScriptWriterError,
)
from tests.fakes import use_env

ENV_TEXT = "\n".join(
    [
        "ELEVEN_LABS=test-eleven",
        "SARVAM_API=test-sarvam",
        "GEMINI_API_KEY=test-google",
        "OPENROUTER_API_KEY=test-openrouter",
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
    store = ScriptHistoryStore(tmp_path / "scripts")
    monkeypatch.setattr(shared, "script_history", store)
    monkeypatch.setattr(shared, "script_writer", ScriptWriter(store))
    client = TestClient(api.app)
    use_env(monkeypatch, ENV_TEXT)
    return client


def _swap_writer(monkeypatch, write) -> None:
    """A real ScriptWriter over the test's history, with no model behind it.

    Only the generation step is faked, so the history the writer reads and
    records is the same store the batch route records the operator's rows to.
    """
    monkeypatch.setattr(shared, "script_writer", ScriptWriter(shared.script_history, write=write))


def _stub_writer(monkeypatch, captured: dict | None = None):
    def fake(*, brief, language, publish_date, items, recent, history, settings):
        if captured is not None:
            captured.update(
                brief=brief, language=language, publish_date=publish_date,
                items=items, recent=recent, history=history,
            )
        return [
            DraftScript(title=item.title, script=f"{item.key} script", key=item.key)
            for item in items
        ]

    _swap_writer(monkeypatch, fake)


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
    assert body["warnings"] == []


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

    _swap_writer(monkeypatch, fake)

    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17"},
    )
    assert resp.status_code == 502
    assert "Gemini said no" in resp.json()["detail"]


def test_an_unrepaired_set_comes_back_with_its_clashes_as_warnings(client, monkeypatch) -> None:
    """The Video pane shows the twelve and the warnings, so the operator fixes
    the one clash rather than re-running the day."""
    def fake(*, items, **_kwargs):
        raise ScriptRepairError(
            "still clashing",
            drafts=[DraftScript(title=i.title, script=f"{i.key} script", key=i.key)
                    for i in items],
            violations=[Violation(item_key="Taurus", rule="colour_repeat",
                                  detail="colour लाल already used today", hard=True)],
        )

    _swap_writer(monkeypatch, fake)

    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == len(ZODIAC_SIGNS)
    assert len(body["warnings"]) == 1 and "Taurus" in body["warnings"][0]
    assert all(isinstance(w, str) for w in body["warnings"])


def test_an_unknown_item_set_is_refused(client, monkeypatch) -> None:
    _stub_writer(monkeypatch)

    resp = client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17", "item_set": "weekly"},
    )
    assert resp.status_code == 400


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

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

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

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    client.post(
        "/video/scripts/generate",
        data={"brief": "Daily horoscope", "publish_date": "2026-08-17",
              "language": "hi-IN", "category": "sunsign"},
    )
    assert [d.script for d in shared.script_history.recent(
        category="sunsign", language="hi-IN")] == [f"{s.key} script" for s in ZODIAC_SIGNS]

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "मैंने इसे बदल दिया", "video_title": "मेष"}]),
              "publish_date": "2026-08-17", "category": "sunsign", "language": "hi-IN"},
    )
    assert resp.status_code == 202, resp.text

    recorded = shared.script_history.recent(category="sunsign", language="hi-IN")
    assert [(d.title, d.script) for d in recorded] == [("2026-08-17 मेष", "मैंने इसे बदल दिया")]


def test_history_records_the_title_intake_scrubbed(client, monkeypatch) -> None:
    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "एक स्क्रिप्ट", "video_title": "promo: one"}]),
              "publish_date": "2026-08-17", "category": "sunsign", "language": "hi-IN"},
    )
    assert resp.status_code == 202, resp.text

    state = client.get(f"/video/heygen/batch/{resp.json()['batch_id']}").json()
    scrubbed = state["rows"][0]["video_title"]
    assert scrubbed != "promo: one"
    recorded = shared.script_history.recent(category="sunsign", language="hi-IN")
    assert [d.key for d in recorded] == [scrubbed.replace(" ", "_")]


def test_a_spreadsheet_batch_records_no_history(client, monkeypatch) -> None:
    """An Excel batch carries no category or language to file it under, and its
    scripts were not written by the model that would be told to avoid them."""
    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

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
    assert shared.script_history.recent(category="sunsign", language="hi-IN") == []


def test_rows_without_a_history_key_still_render(client, monkeypatch) -> None:
    """Recording is a side effect of a run, never a precondition for one."""
    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

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


def test_the_rows_submitted_keep_their_item_key_so_facts_survive(client, monkeypatch) -> None:
    """The re-record overwrites the day written at generation time.

    A batch row carries only the title, so the key has to be mapped back from
    it. Dropped, the rewritten record has no item key, `facts()` skips it, and
    the day vanishes from the uniqueness window — silently, because the prose
    window does not need a key and goes on looking healthy.
    """
    _stub_writer(monkeypatch)

    async def fake_batch(**_kwargs):
        pass

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    script = (
        "[warm] मेष राशि के जातकों... आज नई शुरुआत, ऊर्जा और नेतृत्व!\n\n"
        "[reassuring] एक अवसर मिलेगा। "
        "[calm] स्वास्थ्य उत्तम रहेगा और मानसिक प्रसन्नता बनी रहेगी। "
        "[bright] शुभ रंग: लाल | जादुई अंक: सत्रह (१७)।\n\n"
        "[uplifting] आज का दिन शुभ है।"
    )
    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": script, "video_title": "मेष"}]),
              "publish_date": "2026-08-17", "category": "sunsign", "language": "hi-IN"},
    )
    assert resp.status_code == 202, resp.text

    facts = shared.script_history.facts(category="sunsign", language="hi-IN")
    assert "Aries" in facts, "the Devanagari title must map back to the item key"
    assert facts["Aries"].colours == ("लाल",)
    assert facts["Aries"].numbers == (17,)


def test_a_horoscope_batch_with_an_unmapped_title_is_refused(client, monkeypatch) -> None:
    """The writer's refusal reaches the operator as a 400, before any render."""
    async def fake_batch(**_kwargs):
        raise AssertionError("a refused batch must not start rendering")

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "एक", "video_title": "मेष"},
                                  {"script": "दो", "video_title": "not-a-sign"}]),
              "publish_date": "2026-08-17", "category": "sunsign", "language": "hi-IN"},
    )

    assert resp.status_code == 400
    assert "not-a-sign" in resp.json()["detail"]


def test_a_batch_with_duplicate_titles_is_refused_and_records_nothing(client, monkeypatch) -> None:
    """Two rows filed under one name would overwrite each other on the NAS."""
    started: list = []

    async def fake_batch(**kwargs):
        started.append(kwargs)

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "एक", "video_title": "मेष"},
                                  {"script": "दो", "video_title": "मेष.mp4"}]),
              "publish_date": "2026-08-17", "category": "sunsign", "language": "hi-IN"},
    )

    assert resp.status_code == 400
    assert "मेष" in resp.json()["detail"]
    assert started == []
    assert shared.script_history.recent(category="sunsign", language="hi-IN") == []


def test_a_refused_batch_names_every_bad_row_in_its_detail(client, monkeypatch) -> None:
    """One fix-up pass, not one resubmit per bad row."""
    async def fake_batch(**_kwargs):
        raise AssertionError("a refused batch must not start rendering")

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "x" * (MAX_SCRIPT_CHARS + 1), "video_title": "Aries"},
                                  {"script": "एक", "video_title": "Taurus"},
                                  {"script": "दो", "video_title": ".mp4"}])},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "'Aries'" in detail and str(MAX_SCRIPT_CHARS) in detail
    assert "'.mp4'" in detail and "video_title" in detail
    assert "Taurus" not in detail


def test_the_batch_overlay_and_music_settings_reach_every_spec(client, monkeypatch) -> None:
    started: list = []

    async def fake_batch(**kwargs):
        started.append(kwargs)

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        files=_image(),
        data={"rows": json.dumps([{"script": "एक", "video_title": "a"},
                                  {"script": "दो", "video_title": "b"}]),
              "include_overlay": "false", "include_music": "true"},
    )

    assert resp.status_code == 202, resp.text
    (kwargs,) = started
    assert {(i.spec.include_overlay, i.spec.include_music) for i in kwargs["items"]} == {(False, True)}


def test_a_batch_with_a_picked_photo_and_no_image_is_accepted(client, monkeypatch) -> None:
    started: list = []

    async def fake_batch(**kwargs):
        started.append(kwargs)

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        data={"rows": json.dumps([{"script": "एक", "video_title": "a"},
                                  {"script": "दो", "video_title": "b"}]),
              "talking_photo_id": "tp-picked"},
    )

    assert resp.status_code == 202, resp.text
    (kwargs,) = started
    assert kwargs["talking_photo_id"] == "tp-picked"
    assert kwargs["image_bytes"] == b""
    assert {i.spec.talking_photo_id for i in kwargs["items"]} == {"tp-picked"}


@pytest.mark.parametrize("photo", [{}, {"talking_photo_id": "   "}])
def test_a_batch_with_no_photo_is_refused_as_a_single_video_is(client, monkeypatch, photo) -> None:
    started: list = []

    async def fake_batch(**kwargs):
        started.append(kwargs)

    monkeypatch.setattr(video_batch_routes, "run_video_batch_job", fake_batch)

    resp = client.post(
        "/video/heygen/batch",
        data={"rows": json.dumps([{"script": "एक", "video_title": "a"}]), **photo},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "provide either an image file or a talking_photo_id"
    assert started == []
