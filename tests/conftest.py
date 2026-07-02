import pytest


@pytest.fixture(autouse=True)
def _disable_app_auth(monkeypatch):
    """Auth is enabled globally whenever APP_PASSWORD is set. Since the app
    loads a developer .env at import time, that value would leak into the test
    process and 401 every gated route. Clear it by default so tests run with
    auth off; test_auth opts back in explicitly with monkeypatch.setenv."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
