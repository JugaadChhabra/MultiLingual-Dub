"""The OpenRouter client turns every failure into a status the rest of the app reads.

retry.py retries on 429 and 5xx, errors.py reports 402 as spent credit; both
read ``status_code``. The case worth pinning is OpenRouter's 200 with an error
body, which would otherwise be parsed as a reply with no content.
"""
from __future__ import annotations

import json

import httpx
import pytest

from services.errors import classify
from services.openrouter import OpenRouterClient, OpenRouterError
from services.retry import is_retryable


def _respond(monkeypatch, status: int, body: object, captured: dict | None = None) -> None:
    def fake_post(url, *, headers, json, timeout):
        if captured is not None:
            captured.update(url=url, headers=headers, json=json)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.openrouter.httpx.post", fake_post)


def _call() -> str:
    return OpenRouterClient("key").complete_json(
        model="google/gemini-2.5-flash", system="sys", prompt="hi", schema={"type": "object"}
    )


def test_returns_the_message_content_and_asks_for_strict_json(monkeypatch) -> None:
    captured: dict = {}
    _respond(monkeypatch, 200, {"choices": [{"message": {"content": '{"Aries": "x"}'}}]}, captured)

    assert json.loads(_call()) == {"Aries": "x"}
    assert captured["headers"]["Authorization"] == "Bearer key"
    body = captured["json"]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"]["require_parameters"] is True
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_spent_credit_is_a_402_the_classifier_names(monkeypatch) -> None:
    _respond(monkeypatch, 402, {"error": {"code": 402, "message": "Insufficient credits"}})

    with pytest.raises(OpenRouterError) as excinfo:
        _call()

    assert excinfo.value.status_code == 402
    assert not is_retryable(excinfo.value)
    assert classify(excinfo.value, provider="openrouter").kind == "quota"


def test_an_error_body_on_a_200_is_still_an_error(monkeypatch) -> None:
    _respond(monkeypatch, 200, {"error": {"code": 502, "message": "upstream provider failed"}})

    with pytest.raises(OpenRouterError) as excinfo:
        _call()

    assert excinfo.value.status_code == 502
    assert is_retryable(excinfo.value)


def test_a_rate_limit_is_retried(monkeypatch) -> None:
    _respond(monkeypatch, 429, {"error": {"code": 429, "message": "rate limited"}})

    with pytest.raises(OpenRouterError) as excinfo:
        _call()

    assert is_retryable(excinfo.value)
