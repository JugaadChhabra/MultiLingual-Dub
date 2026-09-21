from __future__ import annotations

import httpx

from services.google_translate import GoogleTranslateSettings
from services.retry import retry_call
from services.runtime_config import MissingSettingError

# Languages Sarvam does not cover, routed to the official Google Cloud
# Translation API. This is the set that used to go through the unofficial free
# web endpoint; the languages are unchanged, only the provider is.
GOOGLE_TRANSLATE_LANGUAGES = {"fr", "de", "es", "ru", "pt"}

_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def normalize_language_code(language_code: str) -> str:
    normalized = language_code.strip().lower().replace("_", "-")
    if not normalized:
        return ""
    return normalized.split("-", 1)[0]


def should_use_google_translate(target_language_code: str) -> bool:
    return normalize_language_code(target_language_code) in GOOGLE_TRANSLATE_LANGUAGES


def _post_translate(
    *, api_key: str, text: str, target: str, source: str | None
) -> dict:
    """One call to the v2 REST endpoint. Isolated so tests can stub the network
    and so retry_call has a single unit to re-run."""
    body: dict[str, str] = {"q": text, "target": target, "format": "text"}
    if source:
        body["source"] = source
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(_ENDPOINT, params={"key": api_key}, json=body)
        resp.raise_for_status()
        return resp.json()


def _extract_translated_text(response: dict) -> str:
    translations = response.get("data", {}).get("translations")
    if isinstance(translations, list) and translations:
        first = translations[0]
        if isinstance(first, dict):
            text = first.get("translatedText")
            if isinstance(text, str):
                return text
    raise RuntimeError(f"Unexpected Google translation response shape: {response!r}")


def translate_text_official(
    text: str,
    target_language_code: str,
    *,
    settings: GoogleTranslateSettings,
    source_language_code: str = "auto",
) -> str:
    normalized_target = normalize_language_code(target_language_code)
    normalized_source = normalize_language_code(source_language_code)

    if normalized_source and normalized_source != "auto" and normalized_source == normalized_target:
        return text

    if not normalized_target:
        raise ValueError("Target language code cannot be empty")

    if normalized_target not in GOOGLE_TRANSLATE_LANGUAGES:
        raise ValueError(
            f"Unsupported Google Cloud Translation target language: {normalized_target}"
        )

    if not settings.api_key:
        raise MissingSettingError(["GOOGLE_TRANSLATE_API_KEY"])

    source = normalized_source if normalized_source and normalized_source != "auto" else None

    def _call_once() -> str:
        response = _post_translate(
            api_key=settings.api_key,
            text=text,
            target=normalized_target,
            source=source,
        )
        translated = _extract_translated_text(response)
        if not translated.strip():
            raise RuntimeError("Google Cloud Translation returned empty translation")
        return translated

    return retry_call(_call_once, operation="Google Cloud Translation")
