from __future__ import annotations

from services.free_translate import should_use_free_translate, translate_text_free
from services.translate import translate_text
from services.runtime_config import RuntimeConfig


def translate_with_fallback(
    text: str,
    *,
    runtime_config: RuntimeConfig | None = None,
    target_language_code: str,
    source_language_code: str = "auto",
) -> str:
    if should_use_free_translate(target_language_code):
        return translate_text_free(
            text,
            runtime_config=runtime_config,
            target_language_code=target_language_code,
            source_language_code=source_language_code,
        )

    try:
        return translate_text(
            text,
            runtime_config=runtime_config,
            target_language_code=target_language_code,
            source_language_code=source_language_code,
        )
    except Exception as exc:
        if "Source and target languages must be different" in str(exc):
            return text
        raise
