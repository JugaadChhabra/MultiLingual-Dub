from __future__ import annotations

import pytest

from services.google_translate import GoogleTranslateSettings
from services.runtime_config import MissingSettingError
from services.translation import google_official

GOOGLE = GoogleTranslateSettings(api_key="google-key")


def test_should_use_google_translate_for_supported_language_codes() -> None:
    assert google_official.should_use_google_translate("fr")
    assert google_official.should_use_google_translate("fr-FR")
    assert google_official.should_use_google_translate("pt-BR")
    assert not google_official.should_use_google_translate("hi-IN")


def test_translate_text_official_raises_for_unsupported_language() -> None:
    with pytest.raises(ValueError, match="Unsupported Google Cloud Translation"):
        google_official.translate_text_official("hello", "hi-IN", settings=GOOGLE)


def test_translate_text_official_raises_when_key_missing() -> None:
    with pytest.raises(MissingSettingError, match="GOOGLE_TRANSLATE_API_KEY"):
        google_official.translate_text_official(
            "hello", "fr", settings=GoogleTranslateSettings(api_key="")
        )


def test_translate_text_official_posts_and_extracts_translation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(*, api_key: str, text: str, target: str, source):
        captured.update(api_key=api_key, text=text, target=target, source=source)
        return {"data": {"translations": [{"translatedText": "bonjour"}]}}

    monkeypatch.setattr(google_official, "_post_translate", fake_post)

    translated = google_official.translate_text_official("hello", "fr-FR", settings=GOOGLE)

    assert translated == "bonjour"
    assert captured["api_key"] == "google-key"
    assert captured["text"] == "hello"
    assert captured["target"] == "fr"
    # "auto" source is sent as None so the API auto-detects.
    assert captured["source"] is None


def test_translate_text_official_passes_explicit_source(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(*, api_key, text, target, source):
        captured["source"] = source
        return {"data": {"translations": [{"translatedText": "bonjour"}]}}

    monkeypatch.setattr(google_official, "_post_translate", fake_post)

    google_official.translate_text_official(
        "hello", "fr", settings=GOOGLE, source_language_code="en-US"
    )

    assert captured["source"] == "en"


def test_translate_text_official_returns_input_for_same_source_target(monkeypatch) -> None:
    def fail_if_called(**kwargs):
        raise AssertionError("no network call for a no-op translation")

    monkeypatch.setattr(google_official, "_post_translate", fail_if_called)

    result = google_official.translate_text_official(
        "bonjour", "fr", settings=GOOGLE, source_language_code="fr-FR"
    )

    assert result == "bonjour"


def test_translate_text_official_raises_for_empty_translation(monkeypatch) -> None:
    monkeypatch.setattr(
        google_official,
        "_post_translate",
        lambda **kwargs: {"data": {"translations": [{"translatedText": "   "}]}},
    )

    with pytest.raises(RuntimeError, match="returned empty translation"):
        google_official.translate_text_official("hello", "fr", settings=GOOGLE)
