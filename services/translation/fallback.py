from __future__ import annotations

from services.google_translate import GoogleTranslateSettings
from services.sarvam import SarvamSettings
from services.translation.google_official import (
    should_use_google_translate,
    translate_text_official,
)
from services.translation.sarvam import translate_text


def translate_with_fallback(
    text: str,
    *,
    settings: SarvamSettings,
    google_translate: GoogleTranslateSettings,
    target_language_code: str,
    source_language_code: str = "auto",
) -> str:
    if should_use_google_translate(target_language_code):
        return translate_text_official(
            text,
            target_language_code=target_language_code,
            settings=google_translate,
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
