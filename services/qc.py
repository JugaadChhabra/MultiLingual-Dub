from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import google.genai as genai

from services.retry import retry_call

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "bn-IN": "Bengali",
    "en-IN": "English",
    "gu-IN": "Gujarati",
    "hi-IN": "Hindi",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "od-IN": "Odia",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}

DEFAULT_QC_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]


class QCError(Exception):
    pass


def get_gemini_api_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY environment variable")
    return api_key


def _get_qc_models() -> list[str]:
    raw = os.getenv("GEMINI_QC_MODELS", "").strip()
    if raw:
        models = [item.strip() for item in raw.split(",") if item.strip()]
        if models:
            return models
    return DEFAULT_QC_MODELS


def _get_qc_log_path() -> Path:
    raw = os.getenv("QC_LOG_PATH", "data/qc/qc-log.jsonl").strip()
    return Path(raw)


def _log_qc_sample(
    *,
    model: str,
    original_text: str,
    input_translations: dict[str, str],
    output_translations: dict[str, str],
    target_languages: list[str],
    metadata: dict[str, object] | None,
) -> None:
    payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "model": model,
        "original_text": original_text,
        "input_translations": input_translations,
        "output_translations": output_translations,
        "target_languages": target_languages,
    }
    if metadata:
        payload["metadata"] = metadata

    path = _get_qc_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("QC: failed to write log sample to %s: %s", path, exc)


def _parse_response_json(response_text: str) -> dict[str, str]:
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[7:-3]
    elif text.startswith("```"):
        text = text[3:-3]
    return json.loads(text)


def qc_translations_batch(
    original_text: str,
    translations: dict[str, str],
    target_languages: list[str],
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, str]:
    """
    QC multiple translations at once using Gemini.
    
    :param original_text: English source text
    :param translations: Dict of {language_code: translated_text}
    :param target_languages: List of language codes
    :param metadata: Optional metadata to store with QC log entries
    :return: Dict of {language_code: corrected_text}
    """
    if not target_languages or not translations:
        return translations

    try:
        api_key = get_gemini_api_key()
        client = genai.Client(api_key=api_key)
        models = _get_qc_models()
        
        # Build language descriptions
        lang_descs = ", ".join(
            f"{lang} ({LANGUAGE_NAMES.get(lang, lang)})"
            for lang in target_languages
        )
        
        # Build JSON input
        translations_json = json.dumps(translations, ensure_ascii=False, indent=2)
        logger.info(f"Input translations JSON:\n{translations_json}")
        
        prompt = f"""You are a QC expert in Indian languages: {lang_descs}.

Original English text: "{original_text}"

Translations to QC:
{translations_json}

Task: Fix common translation errors in all target languages. Focus on:
1. Transliterated English words: Minimize vowels - "sum" should be सम not सुम्, remove trailing halants completely
2. Extra vowels: Remove unnecessary vowel marks added by translation service
3. Vowel sign errors: Fix incorrect or redundant vowel diacritics (maatras)
4. Halants/Virama: Remove trailing halants (्) or equivalent marks at end of words - they should NOT appear at word endings
5. Anusvara issues: Fix unnecessary anusvara (्) or nasal marks at word endings
6. Consonant clusters: Correct improper consonant combinations
7. Unnatural repetitions: Remove repeated syllables or sounds

CRITICAL: English technical terms and proper nouns that are transliterated should have MINIMAL characters - no extra vowels, no trailing halants.

Each language has its own script rules - apply these principles to Bengali, Gujarati, Hindi, Kannada, Malayalam, Marathi, Odia, Punjabi, Tamil, and Telugu scripts accordingly.

Return ONLY the corrected translations in JSON format, same structure as input.
Return valid JSON only, no other text:"""
        
        last_exc: Exception | None = None

        for model in models:
            try:
                response = retry_call(
                    lambda: client.models.generate_content(model=model, contents=prompt),
                    operation=f"Gemini QC ({model})",
                )
                response_text = response.text.strip()
                corrected = _parse_response_json(response_text)

                # Validate response has all languages
                for lang in target_languages:
                    if lang not in corrected:
                        logger.warning(f"QC: language {lang} missing in response, using original")
                        corrected[lang] = translations.get(lang, "")

                corrected_json = json.dumps(corrected, ensure_ascii=False, indent=2)
                logger.info(f"Corrected translations JSON:\n{corrected_json}")
                logger.info(f"QC successful for {len(corrected)} languages using {model}")

                _log_qc_sample(
                    model=model,
                    original_text=original_text,
                    input_translations=translations,
                    output_translations=corrected,
                    target_languages=target_languages,
                    metadata=metadata,
                )
                return corrected
            except Exception as exc:
                last_exc = exc
                logger.warning("QC: model %s failed: %s", model, exc)

        if last_exc:
            raise QCError(f"Gemini QC failed after {len(models)} models: {last_exc}") from last_exc
        raise QCError("Gemini QC failed: no models available")

    except QCError:
        raise
    except Exception as e:
        logger.error(f"QC: Gemini API error: {e}")
        raise QCError(f"Gemini QC failed: {e}") from e
