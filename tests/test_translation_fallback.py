from services import translation


def test_translate_with_fallback_returns_source_on_language_mismatch(monkeypatch) -> None:
    def fake_translate_text(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (runtime_config, target_language_code, source_language_code)
        raise RuntimeError("Source and target language cannot be same")

    monkeypatch.setattr(translation, "translate_text", fake_translate_text)

    source = "kem cho"
    translated = translation.translate_with_fallback(
        source,
        target_language_code="hi-IN",
        source_language_code="auto",
    )

    assert translated == source


def test_translate_with_fallback_raises_for_other_errors(monkeypatch) -> None:
    def fake_translate_text(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (text, runtime_config, target_language_code, source_language_code)
        raise RuntimeError("HTTP 400 Bad Request: Unsupported language pair")

    monkeypatch.setattr(translation, "translate_text", fake_translate_text)

    try:
        translation.translate_with_fallback(
            "hello",
            target_language_code="xx-XX",
            source_language_code="auto",
        )
    except RuntimeError as exc:
        assert "Unsupported language pair" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError to be raised")


def test_translate_with_fallback_parses_response_body_for_same_language(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.text = '{"error":{"message":"Source and target language code should be different"}}'

        def json(self):
            return {"error": {"message": "Source and target language code should be different"}}

    class FakeHTTPError(RuntimeError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.response = FakeResponse()

    def fake_translate_text(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (text, runtime_config, target_language_code, source_language_code)
        raise FakeHTTPError("HTTP 400 Bad Request")

    monkeypatch.setattr(translation, "translate_text", fake_translate_text)

    source = "नमस्ते दुनिया"
    translated = translation.translate_with_fallback(
        source,
        target_language_code="hi-IN",
        source_language_code="auto",
    )

    assert translated == source
