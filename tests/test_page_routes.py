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

    for path in ("/", "/heygen"):
        response = client.get(path)
        assert response.status_code == 200, path
        cache_control = response.headers.get("cache-control", "")
        assert "no-cache" in cache_control, f"{path} -> {cache_control!r}"
        # an ETag keeps revalidation cheap (304 instead of a full re-download)
        assert response.headers.get("etag"), path


def test_static_assets_stay_cacheable() -> None:
    """Only the HTML opts out. Versioned assets should still cache normally."""
    client = TestClient(api.app)

    for path in ("/static/app.js", "/static/wave.js", "/static/audio.js", "/static/style.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-cache" not in response.headers.get("cache-control", ""), path


def test_shared_modules_load_before_their_first_use() -> None:
    """wave.js and audio.js expose plain globals, so load order is load-bearing."""
    client = TestClient(api.app)

    # Studio: both modules must precede the page script that consumes them.
    index = client.get("/").text
    for module in ("/static/wave.js", "/static/audio.js"):
        assert module in index
        assert index.index(module) < index.index("/static/app.js")

    # Video Studio: the page script is inline, so compare against first use.
    heygen = client.get("/heygen").text
    for module in ("/static/wave.js", "/static/audio.js"):
        assert module in heygen
    assert heygen.index("/static/wave.js") < heygen.index("AutoDubWave.create")
    assert heygen.index("/static/audio.js") < heygen.index("AutoDubAudio.create")
