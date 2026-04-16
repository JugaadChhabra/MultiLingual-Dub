from __future__ import annotations

from deep_translator import GoogleTranslator

from services.retry import retry_call
from services.runtime_config import RuntimeConfig

FREE_TRANSLATE_LANGUAGES = {"fr", "de", "es", "ru", "pt"}


def normalize_language_code(language_code: str) -> str:
    normalized = language_code.strip().lower().replace("_", "-")
    if not normalized:
        return ""
    return normalized.split("-", 1)[0]


def should_use_free_translate(target_language_code: str) -> bool:
    return normalize_language_code(target_language_code) in FREE_TRANSLATE_LANGUAGES


def translate_text_free(
    text: str,
    target_language_code: str,
    *,
    runtime_config: RuntimeConfig | None = None,
    source_language_code: str = "auto",
) -> str:
    # Keep signature aligned with other translation providers.
    _ = runtime_config

    normalized_target = normalize_language_code(target_language_code)
    normalized_source = normalize_language_code(source_language_code)

    if normalized_source and normalized_source != "auto" and normalized_source == normalized_target:
        return text

    if not normalized_target:
        raise ValueError("Target language code cannot be empty")

    if normalized_target not in FREE_TRANSLATE_LANGUAGES:
        raise ValueError(
            f"Unsupported in-process free translation target language: {normalized_target}"
        )

    def _call_once() -> str:
        translated = GoogleTranslator(source="auto", target=normalized_target).translate(text)
        if not isinstance(translated, str) or not translated.strip():
            raise RuntimeError("In-process free translator returned empty translation")
        return translated

    return retry_call(_call_once, operation="in-process free translate")
