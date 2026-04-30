from services.sarvam import get_sarvam_client
from services.retry import retry_call
from services.runtime_config import RuntimeConfig


def _extract_translated_text(response) -> str:
    if isinstance(response, str):
        return response

    if isinstance(response, dict):
        for key in ("translated_text", "translation", "text", "output"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value

        translations = response.get("translations")
        if isinstance(translations, list) and translations:
            first = translations[0]
            if isinstance(first, dict):
                text = first.get("text")
                if isinstance(text, str) and text.strip():
                    return text

    for attr in ("translated_text", "translation", "text"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value.strip():
            return value

    return str(response)


def translate_text(
    text: str,
    target_language_code: str,
    *,
    runtime_config: RuntimeConfig | None = None,
    source_language_code: str = "auto",
    speaker_gender: str = "Male",
    mode: str = "formal",
    model: str = "mayura:v1",
    numerals_format: str = "native",
) -> str:
    def _call():
        client = get_sarvam_client(runtime_config=runtime_config)
        return client.text.translate(
            input=text,
            source_language_code=source_language_code,
            target_language_code=target_language_code,
            speaker_gender=speaker_gender,
            mode=mode,
            model=model,
            numerals_format=numerals_format,
        )

    response = retry_call(_call, operation="Sarvam translate")
    return _extract_translated_text(response)


if __name__ == "__main__":
    sample_text = "Hello! This is a sample translation."
    translated = translate_text(sample_text, target_language_code="hi-IN")
    print(translated)
