from __future__ import annotations

import json
import logging
import os

import google.genai as genai

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


class QCError(Exception):
    pass


def get_gemini_api_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY environment variable")
    return api_key


def qc_translations_batch(
    original_text: str,
    translations: dict[str, str],
    target_languages: list[str],
) -> dict[str, str]:
    """
    QC multiple translations at once using Gemini.
    
    :param original_text: English source text
    :param translations: Dict of {language_code: translated_text}
    :param target_languages: List of language codes
    :return: Dict of {language_code: corrected_text}
    """
    if not target_languages or not translations:
        return translations

    try:
        api_key = get_gemini_api_key()
        client = genai.Client(api_key=api_key)
        
        # Build language descriptions
        lang_descs = ", ".join(
            f"{lang} ({LANGUAGE_NAMES.get(lang, lang)})"
            for lang in target_languages
        )
        
        # Build JSON input
        translations_json = json.dumps(translations, ensure_ascii=False, indent=2)
        
        prompt = f"""You are a QC expert in Indian languages: {lang_descs}.

Original English text: "{original_text}"

Translations to QC:
{translations_json}

Task: Check each translation for extra vowels, redundant maatras, or unnatural repetitions.
- Remove extra vowels/maatras that were added by the translation service
- Keep the meaning and naturalness intact
- Return ONLY the corrected translations in JSON format, same structure as input

Return valid JSON only, no other text:"""
        
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        response_text = response.text.strip()
        
        # Try to extract JSON from response
        if response_text.startswith("```json"):
            response_text = response_text[7:-3]  # Remove ```json and ```
        elif response_text.startswith("```"):
            response_text = response_text[3:-3]  # Remove ``` markers
        
        corrected = json.loads(response_text)
        
        # Validate response has all languages
        for lang in target_languages:
            if lang not in corrected:
                logger.warning(f"QC: language {lang} missing in response, using original")
                corrected[lang] = translations.get(lang, "")
        
        logger.info(f"QC successful for {len(corrected)} languages")
        return corrected
        
    except json.JSONDecodeError as e:
        logger.error(f"QC: failed to parse JSON response: {e}")
        raise QCError(f"Invalid JSON from Gemini: {e}") from e
    except Exception as e:
        logger.error(f"QC: Gemini API error: {e}")
        raise QCError(f"Gemini QC failed: {e}") from e
