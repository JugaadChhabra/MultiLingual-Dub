from fastapi.testclient import TestClient

from api import routes as api


def test_html_pages_are_never_heuristically_cached() -> None:
    """The HTML must revalidate on every load.

    Without an explicit Cache-Control, browsers apply heuristic freshness and can
    serve a stale page against freshly deployed JS. The markup then lacks the ids
    the new script expects and the page dies on load, which is not a failure the
    operator can diagnose. The ?v= query on the asset tags cannot save this,
    because it lives inside the very HTML that went stale.
    """
    client = TestClient(api.app)

    for path in ("/", "/videogen"):
        response = client.get(path)
        assert response.status_code == 200, path
        cache_control = response.headers.get("cache-control", "")
        assert "no-cache" in cache_control, f"{path} -> {cache_control!r}"
        # an ETag keeps revalidation cheap (304 instead of a full re-download)
        assert response.headers.get("etag"), path


def test_old_heygen_url_still_reaches_the_video_section() -> None:
    """The video editor has /heygen bookmarked. Renaming the route is fine;
    breaking their bookmark is not, so the old name redirects permanently."""
    client = TestClient(api.app)

    response = client.get("/heygen", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/videogen"

    # and following it lands on the real page
    assert client.get("/heygen").status_code == 200


def test_static_assets_stay_cacheable() -> None:
    """Only the HTML opts out. Versioned assets should still cache normally."""
    client = TestClient(api.app)

    for path in ("/static/app.css", "/static/ui.js", "/static/shell.js",
                 "/static/pane-audio.js", "/static/pane-video.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-cache" not in response.headers.get("cache-control", ""), path


def test_shared_modules_load_before_their_first_use() -> None:
    """ui.js exposes window.UI, which both panes and the shell consume, so load
    order is load-bearing — shell.js boots and must come last."""
    index = TestClient(api.app).get("/").text

    for module in ("/static/ui.js", "/static/pane-audio.js", "/static/pane-video.js", "/static/shell.js"):
        assert module in index, module

    assert index.index("/static/ui.js") < index.index("/static/pane-audio.js")
    assert index.index("/static/ui.js") < index.index("/static/pane-video.js")
    assert index.index("/static/pane-audio.js") < index.index("/static/shell.js")
    assert index.index("/static/pane-video.js") < index.index("/static/shell.js")
