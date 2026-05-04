from services import qc


def test_qc_model_fallback(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_QC_MODELS", "model-a,model-b")
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

    class FakeModels:
        last_instance: "FakeModels | None" = None

        def __init__(self):
            self.calls: list[str] = []
            FakeModels.last_instance = self

        def generate_content(self, model: str, contents: str):
            self.calls.append(model)
            if model == "model-a":
                raise RuntimeError("429 rate limit")
            return FakeResponse('{"hi-IN": "namaste"}')

    class FakeClient:
        def __init__(self, api_key: str):
            self.models = FakeModels()

    monkeypatch.setattr("services.qc.genai.Client", FakeClient)

    result = qc.qc_translations_batch(
        "hello",
        {"hi-IN": "namaste"},
        ["hi-IN"],
    )

    assert result["hi-IN"] == "namaste"
    assert FakeModels.last_instance is not None
    assert FakeModels.last_instance.calls == ["model-a", "model-b"]


def test_qc_prompt_emphasizes_no_unnecessary_english_in_indic_outputs(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_QC_MODELS", "model-a")
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")

    captured_prompt: dict[str, str] = {}

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

    class FakeModels:
        def generate_content(self, model: str, contents: str):
            captured_prompt["value"] = contents
            return FakeResponse('{"hi-IN": "योग को ५ बनाओ"}')

    class FakeClient:
        def __init__(self, api_key: str):
            self.models = FakeModels()

    monkeypatch.setattr("services.qc.genai.Client", FakeClient)

    result = qc.qc_translations_batch(
        "Make the sum 5",
        {"hi-IN": "योग sum को 5 बनाओ"},
        ["hi-IN"],
    )

    assert result["hi-IN"] == "योग को ५ बनाओ"
    prompt = captured_prompt.get("value", "")
    assert "Do NOT keep unnecessary English (Latin-script) words." in prompt
    assert "If both localized and English forms of the same term appear together" in prompt
    assert "For English targets (en-*), keep fluent English and do not transliterate." in prompt
