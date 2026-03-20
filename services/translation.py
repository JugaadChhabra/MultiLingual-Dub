from __future__ import annotations

from services.translate import translate_text
from services.runtime_config import RuntimeConfig


def translate_with_fallback(
    text: str,
    *,
    runtime_config: RuntimeConfig | None = None,
    target_language_code: str,
    source_language_code: str = "auto",
) -> str:
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
