from services import qc
from services.qc import QCSettings
from tests.fakes import patch_openrouter


def test_qc_model_fallback(monkeypatch) -> None:
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")
    settings = QCSettings(api_key="test-key", models=["model-a", "model-b"], enabled=True)

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

    class FakeModels:
        last_instance: "FakeModels | None" = None

        def __init__(self):
            self.calls: list[str] = []
            FakeModels.last_instance = self

        def generate_content(self, model: str, contents: str, config=None):
            self.calls.append(model)
            if model == "model-a":
                raise RuntimeError("429 rate limit")
            return FakeResponse('{"hi-IN": "namaste"}')

    patch_openrouter(monkeypatch, "services.qc.OpenRouterClient", FakeModels)

    result = qc.qc_translations_batch(
        "hello",
        {"hi-IN": "namaste"},
        ["hi-IN"],
        settings=settings,
    )

    assert result["hi-IN"] == "namaste"
    assert FakeModels.last_instance is not None
    assert FakeModels.last_instance.calls == ["model-a", "model-b"]


def test_qc_prompt_emphasizes_no_unnecessary_english_in_indic_outputs(monkeypatch) -> None:
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")
    settings = QCSettings(api_key="test-key", models=["model-a"], enabled=True)

    captured_prompt: dict[str, str] = {}

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            # The QC rules live in the system instruction so they form a
            # cacheable prefix across every row of a batch; only the per-row
            # data goes in contents.
            captured_prompt["value"] = contents
            captured_prompt["system"] = getattr(config, "system_instruction", "") or ""
            return FakeResponse('{"hi-IN": "योग को ५ बनाओ"}')

    patch_openrouter(monkeypatch, "services.qc.OpenRouterClient", FakeModels)

    result = qc.qc_translations_batch(
        "Make the sum 5",
        {"hi-IN": "योग sum को 5 बनाओ"},
        ["hi-IN"],
        settings=settings,
    )

    assert result["hi-IN"] == "योग को ५ बनाओ"
    prompt = captured_prompt.get("system", "")
    assert "Do NOT keep unnecessary English (Latin-script) words." in prompt
    assert "If both localized and English forms of the same term appear together" in prompt
    assert "For English targets (en-*), keep fluent English and do not transliterate." in prompt


def test_qc_reply_is_held_to_the_rows_language_keys(monkeypatch) -> None:
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")
    settings = QCSettings(api_key="test-key", models=["model-a"], enabled=True)
    seen: dict = {}

    class FakeModels:
        def generate_content(self, model: str, contents: str, config=None):
            seen["schema"] = config.response_schema
            return type("R", (), {"text": '{"hi-IN": "a", "ta-IN": "b"}'})()

    patch_openrouter(monkeypatch, "services.qc.OpenRouterClient", FakeModels)

    qc.qc_translations_batch("hi", {"hi-IN": "x", "ta-IN": "y"}, ["hi-IN", "ta-IN"], settings=settings)

    assert seen["schema"]["required"] == ["hi-IN", "ta-IN"]
    assert seen["schema"]["properties"]["ta-IN"] == {"type": "string"}
