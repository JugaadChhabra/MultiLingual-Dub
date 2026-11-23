"""The Compilations proxy, driven against a fake Comp engine (httpx.MockTransport).
No real engine or ffmpeg: what is pinned here is what the pane relies on."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from api import routes as api
from services import comp_engine


@pytest.fixture
def engine(monkeypatch):
    """A fake engine recording every request it gets."""
    seen: list[httpx.Request] = []
    video = bytes(range(256)) * 4  # 1024 bytes

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/queue":
            return httpx.Response(200, json={"jobs": [{"id": 1, "state": "filed"}], "gpu": "Apple"})
        if path == "/api/queue/add":
            body = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "id": 7, "refs": body["refs"]})
        if path == "/api/queue/refile":
            return httpx.Response(409, json={"error": "That comp is not waiting to be filed"})
        if path == "/video":
            rng = request.headers.get("range")
            if rng == "bytes=0-99":
                return httpx.Response(206, content=video[:100], headers={
                    "Content-Type": "video/mp4", "Content-Range": "bytes 0-99/1024",
                    "Accept-Ranges": "bytes", "Content-Length": "100"})
            return httpx.Response(200, content=video, headers={"Content-Type": "video/mp4"})
        if path == "/thumb":
            return httpx.Response(404)
        return httpx.Response(500)

    monkeypatch.setenv("COMP_ENGINE_URL", "http://engine.test")
    monkeypatch.setattr(comp_engine, "_transport", httpx.MockTransport(handler))
    return seen


def test_queue_and_add_reach_the_engine(engine) -> None:
    client = TestClient(api.app)

    assert client.get("/comps/api/queue").json()["gpu"] == "Apple"
    r = client.post("/comps/api/queue/add", json={"refs": ["intro", "rhyme:Hindi/A.mp4"], "options": {}})

    assert r.status_code == 200 and r.json()["id"] == 7
    assert str(engine[-1].url) == "http://engine.test/api/queue/add"
    assert json.loads(engine[-1].content)["refs"] == ["intro", "rhyme:Hindi/A.mp4"]


def test_engine_errors_keep_their_status(engine) -> None:
    """The pane shows the engine's own reason; a 409 must not become a 200 or 500."""
    r = TestClient(api.app).post("/comps/api/queue/refile", json={"id": 3})
    assert r.status_code == 409
    assert "not waiting" in r.json()["error"]


def test_server_settings_are_not_reachable_from_the_browser(engine) -> None:
    """Library folders and parallelism are set on the Mac mini, not in the pane."""
    client = TestClient(api.app)
    assert client.post("/comps/api/config", json={"rhymes_folder": "/"}).status_code == 404
    assert client.post("/comps/api/queue/parallel", json={"n": 4}).status_code == 404
    assert engine == []


def test_video_passes_range_through_for_seeking(engine) -> None:
    r = TestClient(api.app).get("/comps/video", params={"ref": "job:1"}, headers={"Range": "bytes=0-99"})

    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-99/1024"
    assert len(r.content) == 100
    assert engine[-1].headers["range"] == "bytes=0-99"
    assert engine[-1].url.params["ref"] == "job:1"


def test_missing_thumb_is_a_plain_404(engine) -> None:
    assert TestClient(api.app).get("/comps/thumb", params={"ref": "rhyme:x"}).status_code == 404


def test_engine_down_says_so(monkeypatch) -> None:
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(comp_engine, "_transport", httpx.MockTransport(refuse))
    client = TestClient(api.app)

    for r in (client.get("/comps/api/queue"), client.get("/comps/video", params={"ref": "job:1"})):
        assert r.status_code == 503
        assert r.json()["engine"] is False
