from __future__ import annotations

import pytest

from services import free_translate


def test_should_use_free_translate_for_supported_language_codes() -> None:
    assert free_translate.should_use_free_translate("fr")
    assert free_translate.should_use_free_translate("fr-FR")
    assert free_translate.should_use_free_translate("pt-BR")
    assert not free_translate.should_use_free_translate("hi-IN")


def test_translate_text_free_raises_for_unsupported_language() -> None:
    with pytest.raises(ValueError, match="Unsupported in-process free translation"):
        free_translate.translate_text_free("hello", "hi-IN", runtime_config={})


def test_translate_text_free_uses_google_translator_with_normalized_target(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeTranslator:
        def __init__(self, *, source: str, target: str):
            captured["source"] = source
            captured["target"] = target

        def translate(self, text: str) -> str:
            captured["text"] = text
            return "bonjour"

    monkeypatch.setattr(free_translate, "GoogleTranslator", FakeTranslator)
    monkeypatch.setattr(
        free_translate,
        "retry_call",
        lambda func, operation=None: func(),
    )

    translated = free_translate.translate_text_free(
        "hello",
        "fr-FR",
        runtime_config={},
    )

    assert translated == "bonjour"
    assert captured["source"] == "auto"
    assert captured["target"] == "fr"
    assert captured["text"] == "hello"


def test_translate_text_free_returns_input_for_same_source_target_language(monkeypatch) -> None:
    calls = {"count": 0}

    class FakeTranslator:
        def __init__(self, *args, **kwargs):
            calls["count"] += 1

        def translate(self, text: str) -> str:
            return text

    def fake_retry_call(func, operation=None):
        calls["count"] += 1
        return func()

    monkeypatch.setattr(free_translate, "GoogleTranslator", FakeTranslator)
    monkeypatch.setattr(free_translate, "retry_call", fake_retry_call)

    result = free_translate.translate_text_free(
        "bonjour",
        "fr",
        source_language_code="fr-FR",
        runtime_config={},
    )

    assert result == "bonjour"
    assert calls["count"] == 0


def test_translate_text_free_raises_for_empty_translation(monkeypatch) -> None:
    class FakeTranslator:
        def __init__(self, *, source: str, target: str):
            pass

        def translate(self, text: str) -> str:
            return "   "

    monkeypatch.setattr(free_translate, "GoogleTranslator", FakeTranslator)
    monkeypatch.setattr(
        free_translate,
        "retry_call",
        lambda func, operation=None: func(),
    )

    with pytest.raises(RuntimeError, match="returned empty translation"):
        free_translate.translate_text_free(
            "hello",
            "fr",
            runtime_config={},
        )
