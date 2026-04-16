from __future__ import annotations

import pytest

from services.translation import translate_with_fallback


def test_translate_with_fallback_uses_sarvam_for_existing_languages(monkeypatch) -> None:
    monkeypatch.setattr("services.translation.should_use_free_translate", lambda _lang: False)

    def fake_translate_text(
        text: str,
        target_language_code: str,
        *,
        runtime_config=None,
        source_language_code: str = "auto",
    ) -> str:
        assert text == "hello"
        assert target_language_code == "hi-IN"
        assert source_language_code == "en-IN"
        return "namaste"

    monkeypatch.setattr("services.translation.translate_text", fake_translate_text)

    translated = translate_with_fallback(
        "hello",
        target_language_code="hi-IN",
        source_language_code="en-IN",
    )

    assert translated == "namaste"


def test_translate_with_fallback_uses_free_translate_for_new_languages(monkeypatch) -> None:
    monkeypatch.setattr("services.translation.should_use_free_translate", lambda _lang: True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Sarvam translate_text should not be called for free-translate languages")

    monkeypatch.setattr("services.translation.translate_text", fail_if_called)
    monkeypatch.setattr("services.translation.translate_text_free", lambda *args, **kwargs: "bonjour")

    translated = translate_with_fallback(
        "hello",
        target_language_code="fr",
        source_language_code="en-IN",
    )

    assert translated == "bonjour"


def test_translate_with_fallback_returns_input_when_source_target_same(monkeypatch) -> None:
    monkeypatch.setattr("services.translation.should_use_free_translate", lambda _lang: False)

    def raise_same_language_error(*args, **kwargs):
        raise RuntimeError("Source and target languages must be different")

    monkeypatch.setattr("services.translation.translate_text", raise_same_language_error)

    translated = translate_with_fallback(
        "already translated",
        target_language_code="hi-IN",
        source_language_code="hi-IN",
    )

    assert translated == "already translated"


def test_translate_with_fallback_propagates_non_same_language_errors(monkeypatch) -> None:
    monkeypatch.setattr("services.translation.should_use_free_translate", lambda _lang: False)

    def raise_other_error(*args, **kwargs):
        raise RuntimeError("unexpected translation failure")

    monkeypatch.setattr("services.translation.translate_text", raise_other_error)

    with pytest.raises(RuntimeError, match="unexpected translation failure"):
        translate_with_fallback(
            "hello",
            target_language_code="hi-IN",
            source_language_code="en-IN",
        )
