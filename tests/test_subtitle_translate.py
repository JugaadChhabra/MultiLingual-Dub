from services import subtitle_translate


def test_translate_subtitle_texts_batches_by_char_limit(monkeypatch) -> None:
    calls: list[str] = []

    def fake_translate_with_fallback(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (runtime_config, target_language_code, source_language_code)
        calls.append(text)
        return text

    monkeypatch.setattr(subtitle_translate, "translate_with_fallback", fake_translate_with_fallback)

    texts = [
        "This is cue one with enough words to make the request payload fairly long for batching behavior checks",
        "This is cue two with enough words to make the request payload fairly long for batching behavior checks",
        "This is cue three with enough words to make the request payload fairly long for batching behavior checks",
        "This is cue four with enough words to make the request payload fairly long for batching behavior checks",
    ]

    translated = subtitle_translate.translate_subtitle_texts(
        texts,
        target_language_code="hi-IN",
        max_chars_per_request=220,
    )

    assert translated == texts
    assert len(calls) >= 2


def test_translate_subtitle_texts_falls_back_to_per_cue(monkeypatch) -> None:
    calls: list[str] = []

    def fake_translate_with_fallback(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (runtime_config, target_language_code, source_language_code)
        calls.append(text)
        if "<<<SUB_SPLIT>>>" in text:
            return "flattened translation"
        return f"TR:{text}"

    monkeypatch.setattr(subtitle_translate, "translate_with_fallback", fake_translate_with_fallback)

    source = ["one", "two", "three"]
    translated = subtitle_translate.translate_subtitle_texts(
        source,
        target_language_code="ta-IN",
        max_chars_per_request=500,
    )

    assert translated == ["TR:one", "TR:two", "TR:three"]
    # One batched call + one call per cue when split token is not preserved.
    assert len(calls) == 1 + len(source)


def test_translate_subtitle_texts_falls_back_to_per_cue_when_batch_fails(monkeypatch) -> None:
    calls: list[str] = []

    def fake_translate_with_fallback(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (runtime_config, target_language_code, source_language_code)
        calls.append(text)
        if "<<<SUB_SPLIT>>>" in text:
            raise RuntimeError("HTTP 400 Bad Request")
        return f"TR:{text}"

    monkeypatch.setattr(subtitle_translate, "translate_with_fallback", fake_translate_with_fallback)

    source = ["one", "two", "three"]
    translated = subtitle_translate.translate_subtitle_texts(
        source,
        target_language_code="bn-IN",
        max_chars_per_request=500,
    )

    assert translated == ["TR:one", "TR:two", "TR:three"]
    # One failed batched call + one call per cue in fallback mode.
    assert len(calls) == 1 + len(source)


def test_translate_subtitle_texts_skips_per_cue_on_transient_batch_failure(monkeypatch) -> None:
    calls: list[str] = []

    def fake_translate_with_fallback(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (runtime_config, target_language_code, source_language_code)
        calls.append(text)
        if "<<<SUB_SPLIT>>>" in text:
            raise RuntimeError("HTTP 429 Rate limit exceeded")
        return f"TR:{text}"

    monkeypatch.setattr(subtitle_translate, "translate_with_fallback", fake_translate_with_fallback)

    source = ["one", "two", "three"]
    translated = subtitle_translate.translate_subtitle_texts(
        source,
        target_language_code="en-IN",
        max_chars_per_request=500,
    )

    # Transient batch failures keep source text and avoid per-cue fan-out.
    assert translated == source
    assert len(calls) == 1


def test_translate_subtitle_texts_preserves_source_when_per_cue_fails(monkeypatch) -> None:
    calls: list[str] = []

    def fake_translate_with_fallback(
        text: str,
        *,
        runtime_config=None,
        target_language_code: str,
        source_language_code: str,
    ) -> str:
        _ = (runtime_config, target_language_code, source_language_code)
        calls.append(text)
        if "<<<SUB_SPLIT>>>" in text:
            return "flattened translation"
        if text == "two":
            raise RuntimeError("HTTP 500 Internal server error")
        return f"TR:{text}"

    monkeypatch.setattr(subtitle_translate, "translate_with_fallback", fake_translate_with_fallback)

    source = ["one", "two", "three"]
    translated = subtitle_translate.translate_subtitle_texts(
        source,
        target_language_code="en-IN",
        max_chars_per_request=500,
    )

    assert translated == ["TR:one", "two", "TR:three"]
    assert len(calls) == 1 + len(source)
