"""The talking-photo picker's listing goes through the Renderer seam."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import routes as api
from services.runtime_config import MissingSettingError
from services.video_pipeline.renderer import TalkingPhoto
from tests.fakes import FakeRenderer


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


def _use(monkeypatch, renderer: FakeRenderer) -> None:
    monkeypatch.setattr(api, "_build_renderer", lambda session: renderer)


def test_lists_photos_in_the_shape_the_pane_reads(client, monkeypatch) -> None:
    _use(monkeypatch, FakeRenderer(photos=[
        TalkingPhoto("look-1", "Anchor", "https://x/p.png"),
        TalkingPhoto("look-2"),
    ]))
    resp = client.get("/video/heygen/talking-photos")
    assert resp.status_code == 200
    assert resp.json() == {"items": [
        {"talking_photo_id": "look-1", "name": "Anchor", "preview_url": "https://x/p.png"},
        {"talking_photo_id": "look-2", "name": "", "preview_url": None},
    ]}


def test_missing_key_is_500(client, monkeypatch) -> None:
    def boom(session):
        raise MissingSettingError(["HEYGEN_API_KEY"])
    monkeypatch.setattr(api, "_build_renderer", boom)
    resp = client.get("/video/heygen/talking-photos")
    assert resp.status_code == 500
    assert "HEYGEN_API_KEY" in resp.json()["detail"]


def test_provider_failure_is_502(client, monkeypatch) -> None:
    _use(monkeypatch, FakeRenderer(list_error=RuntimeError("down")))
    resp = client.get("/video/heygen/talking-photos")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "HeyGen list failed: down"
