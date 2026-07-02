from fastapi.testclient import TestClient

from api import routes as api


def test_auth_disabled_when_no_password(monkeypatch) -> None:
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    client = TestClient(api.app)
    assert client.get("/health").status_code == 200
    # a normally-gated route is reachable when auth is off
    assert client.get("/video/heygen/talking-photos").status_code in (200, 500, 502)


def test_gated_route_blocks_without_cookie(monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("APP_SESSION_SECRET", "s3cret")
    client = TestClient(api.app)
    r = client.get("/video/heygen/talking-photos", headers={"Accept": "application/json"})
    assert r.status_code == 401


def test_login_then_access(monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("APP_SESSION_SECRET", "s3cret")
    client = TestClient(api.app)
    bad = client.post("/auth/login", data={"password": "nope"}, follow_redirects=False)
    assert bad.status_code in (302, 303)
    assert api.auth.AUTH_COOKIE_NAME not in bad.cookies
    ok = client.post("/auth/login", data={"password": "hunter2"}, follow_redirects=False)
    assert ok.status_code in (302, 303)
    assert api.auth.AUTH_COOKIE_NAME in client.cookies
    r = client.get("/video/heygen/talking-photos", headers={"Accept": "application/json"})
    assert r.status_code in (200, 500, 502)


def test_health_and_login_always_open(monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("APP_SESSION_SECRET", "s3cret")
    client = TestClient(api.app)
    assert client.get("/health").status_code == 200
