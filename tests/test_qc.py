import json

from services import qc


def test_qc_model_fallback_and_logging(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_QC_MODELS", "model-a,model-b")
    monkeypatch.setenv("QC_LOG_PATH", str(tmp_path / "qc.jsonl"))
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
        metadata={"row_index": 2},
    )

    assert result["hi-IN"] == "namaste"
    assert FakeModels.last_instance is not None
    assert FakeModels.last_instance.calls == ["model-a", "model-b"]

    log_path = tmp_path / "qc.jsonl"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["model"] == "model-b"
    assert payload["original_text"] == "hello"
    assert payload["input_translations"]["hi-IN"] == "namaste"
    assert payload["output_translations"]["hi-IN"] == "namaste"
    assert payload["target_languages"] == ["hi-IN"]
    assert payload["metadata"]["row_index"] == 2
