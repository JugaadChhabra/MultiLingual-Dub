import json
import re

from services import qc


def test_qc_model_fallback_and_logging(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_QC_MODELS", "model-a,model-b")
    monkeypatch.setenv("QC_LOG_SINK", "file")
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

    train_path = qc._get_qc_train_log_path()
    train_lines = train_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(train_lines) == 1
    train_payload = json.loads(train_lines[0])
    assert train_payload["lang_code"] == "hi-IN"
    assert train_payload["model"] == "model-b"
    assert train_payload["messages"][0]["role"] == "system"
    assert "Hindi" in train_payload["messages"][0]["content"]
    assert train_payload["messages"][1]["role"] == "user"
    assert "Original English" in train_payload["messages"][1]["content"]
    assert train_payload["messages"][2]["role"] == "assistant"
    assert train_payload["messages"][2]["content"] == "namaste"


def test_qc_prompt_emphasizes_no_unnecessary_english_in_indic_outputs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_QC_MODELS", "model-a")
    monkeypatch.setenv("QC_LOG_SINK", "file")
    monkeypatch.setenv("QC_LOG_PATH", str(tmp_path / "qc-prompt.jsonl"))
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


def test_qc_s3_logging(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_QC_MODELS", "model-a")
    monkeypatch.setenv("QC_LOG_SINK", "s3")
    monkeypatch.setenv("WASABI_BUCKET", "test-bucket")
    monkeypatch.setenv("WASABI_ENDPOINT_URL", "https://s3.ap-southeast-1.wasabisys.com")
    monkeypatch.setenv("WASABI_REGION", "ap-southeast-1")
    monkeypatch.setenv("WASABI_ACCESS_KEY", "abc")
    monkeypatch.setenv("WASABI_SECRET_KEY", "xyz")
    monkeypatch.setenv("QC_LOG_S3_PREFIX", "qc/")
    monkeypatch.setenv("API_RETRY_MAX_ATTEMPTS", "1")

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

    class FakeModels:
        def generate_content(self, model: str, contents: str):
            return FakeResponse('{"hi-IN": "namaste"}')

    class FakeClient:
        def __init__(self, api_key: str):
            self.models = FakeModels()

    monkeypatch.setattr("services.qc.genai.Client", FakeClient)

    class FakeS3:
        def __init__(self):
            self.put_calls: list[dict[str, object]] = []

        def put_object(self, **kwargs):
            self.put_calls.append(kwargs)

    fake_s3 = FakeS3()
    client_args: dict[str, object] = {}

    def fake_boto3_client(service_name: str, **kwargs):
        assert service_name == "s3"
        client_args.update(kwargs)
        return fake_s3

    monkeypatch.setattr("services.qc.boto3.client", fake_boto3_client)

    qc.qc_translations_batch(
        "hello",
        {"hi-IN": "namaste"},
        ["hi-IN"],
        metadata={"row_index": 2},
    )

    assert len(fake_s3.put_calls) == 2
    raw_call = fake_s3.put_calls[0]
    train_call = fake_s3.put_calls[1]
    assert raw_call["Bucket"] == "test-bucket"
    assert train_call["Bucket"] == "test-bucket"
    assert re.match(r"^qc/raw/\d{4}/\d{2}/\d{2}/\d{6}-[0-9a-f]{32}\.jsonl$", raw_call["Key"])
    assert re.match(r"^qc/train/\d{4}/\d{2}/\d{2}/\d{6}-[0-9a-f]{32}\.jsonl$", train_call["Key"])
    raw_body = raw_call["Body"].decode("utf-8").strip()
    train_body = train_call["Body"].decode("utf-8").strip().splitlines()
    assert json.loads(raw_body)["model"] == "model-a"
    assert json.loads(train_body[0])["lang_code"] == "hi-IN"
