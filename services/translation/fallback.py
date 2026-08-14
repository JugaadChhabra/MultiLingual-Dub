from __future__ import annotations

from services.sarvam import SarvamSettings
from services.translation.free import should_use_free_translate, translate_text_free
from services.translation.sarvam import translate_text


def translate_with_fallback(
    text: str,
    *,
    settings: SarvamSettings,
    target_language_code: str,
    source_language_code: str = "auto",
) -> str:
    if should_use_free_translate(target_language_code):
        return translate_text_free(
            text,
            target_language_code=target_language_code,
            source_language_code=source_language_code,
        )

    try:
        return translate_text(
            text,
            settings=settings,
            target_language_code=target_language_code,
            source_language_code=source_language_code,
        )
    except Exception as exc:
        if "Source and target languages must be different" in str(exc):
            return text
        raise
