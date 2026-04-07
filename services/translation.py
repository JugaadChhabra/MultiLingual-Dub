from __future__ import annotations

from services.translate import translate_text
from services.runtime_config import RuntimeConfig


def _extract_response_error_text(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""

    parts: list[str] = []

    response_text = getattr(response, "text", None)
    if isinstance(response_text, str) and response_text.strip():
        parts.append(response_text.strip())

    json_reader = getattr(response, "json", None)
    if callable(json_reader):
        try:
            payload = json_reader()
        except Exception:
            payload = None

        if payload is not None:
            parts.append(str(payload))
            if isinstance(payload, dict):
                for key in ("message", "detail", "msg"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                error = payload.get("error")
                if isinstance(error, str) and error.strip():
                    parts.append(error.strip())
                elif isinstance(error, dict):
                    for key in ("message", "detail", "msg"):
                        value = error.get(key)
                        if isinstance(value, str) and value.strip():
                            parts.append(value.strip())

    return "\n".join(parts)


def _is_same_language_error(message: str) -> bool:
    text = message.strip().lower()
    if not text:
        return False

    # Sarvam error phrasing can vary between SDK/API versions.
    direct_patterns = (
        "source and target language cannot be same",
        "source and target languages cannot be same",
        "source and target language code should be different",
        "source and target language should be different",
        "source and target must be different",
    )
    if any(pattern in text for pattern in direct_patterns):
        return True

    has_source_target = "source" in text and "target" in text
    has_language = "language" in text or "lang" in text
    has_same_or_different = "same" in text or "different" in text
    return has_source_target and has_language and has_same_or_different


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
        error_text = str(exc)
        response_error_text = _extract_response_error_text(exc)
        if response_error_text:
            error_text = f"{error_text}\n{response_error_text}"

        if _is_same_language_error(error_text):
            return text
        raise
