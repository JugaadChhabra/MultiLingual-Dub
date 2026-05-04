from __future__ import annotations
import json
import logging

import google.genai as genai

from services.languages import LANGUAGE_NAMES, LANGUAGE_SCRIPT_HINTS
from services.retry import retry_call
from services.runtime_config import RuntimeConfig, get_config_value

logger = logging.getLogger(__name__)

DEFAULT_QC_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]


class QCError(Exception):
    pass


def _cfg(name: str, runtime_config: RuntimeConfig | None = None, default: str = "") -> str:
    value = get_config_value(name, runtime_config=runtime_config)
    if value:
        return value
    return default


def get_gemini_api_key(runtime_config: RuntimeConfig | None = None) -> str:
    api_key = _cfg("GEMINI_API_KEY", runtime_config=runtime_config)
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY environment variable")
    return api_key


def _get_qc_models(runtime_config: RuntimeConfig | None = None) -> list[str]:
    raw = _cfg("GEMINI_QC_MODELS", runtime_config=runtime_config).strip()
    if raw:
        models = [item.strip() for item in raw.split(",") if item.strip()]
        if models:
            return models
    return DEFAULT_QC_MODELS


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
    runtime_config: RuntimeConfig | None = None,
    teaching_mode: bool = False,
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
        api_key = get_gemini_api_key(runtime_config=runtime_config)
        client = genai.Client(api_key=api_key)
        models = _get_qc_models(runtime_config=runtime_config)

        lang_descs = ", ".join(
            f"{lang} ({LANGUAGE_NAMES.get(lang, lang)})"
            for lang in target_languages
        )
        non_english_targets = [
            lang for lang in target_languages if not lang.strip().lower().startswith("en")
        ]
        non_english_desc = ", ".join(
            f"{lang} ({LANGUAGE_NAMES.get(lang, lang)})"
            for lang in non_english_targets
        ) or "none"
        script_descs = ", ".join(
            f"{lang}: {LANGUAGE_SCRIPT_HINTS.get(lang, 'native script')}"
            for lang in target_languages
        )

        translations_json = json.dumps(translations, ensure_ascii=False, indent=2)
        logger.info(f"Input translations JSON:\n{translations_json}")

        teaching_instructions = ""
        if teaching_mode:
            teaching_instructions = f"""
SPECIAL TEACHING INSTRUCTIONS:
This is an English learning activity for children. The translations must use a mix of English and the target native language, but keep the explanation natural.
- The target vocabulary word and the English letter being taught MUST remain in English (Latin script). Do not transliterate them.
- Translate the rest of the explanatory sentence naturally and completely into the target language. Do not randomly mix English adjectives, nouns, or verbs into the explanation.
- For alphabet introductions, use a consistent format like "[Letter] से [Word]".
- Correct Example: "A for Apple. An apple is red." -> "A से Apple. Apple लाल होता है।"
- Incorrect Example: "A for Apple. An apple is red and grows on trees." -> "A से Apple. Apple red होता है और trees पर grow करता है।" (Too many English words mixed in)
"""
        else:
            teaching_instructions = f"""
Rules:
1) Preserve the meaning and tone of the original English.
2) For non-English targets ({non_english_desc}):
   - Use natural native-script phrasing for that language.
   - Do NOT keep unnecessary English (Latin-script) words.
   - Exception: keep only unavoidable proper nouns, brand names, or acronyms.
   - If both localized and English forms of the same term appear together in one sentence, keep only the localized form (unless it is an allowed exception).
3) Fix script/orthography issues: redundant vowels, incorrect vowel signs/maatras, trailing halant/virama at word endings, malformed consonant clusters, and accidental repeated syllables.
4) Keep punctuation, placeholders, and numbers appropriate for the target language.
5) For English targets (en-*), keep fluent English and do not transliterate.
6) Output must be valid JSON only (no markdown, no code fences, no commentary); each value must be a plain string.
"""

        prompt = f"""You are a translation quality-control expert for: {lang_descs}.

Original English text:
"{original_text}"

Candidate translations JSON:
{translations_json}

Script reference by language:
{script_descs}

Fix the translations and return corrected JSON using exactly the same keys as input.
{teaching_instructions}

Return only the corrected JSON object."""

        last_exc: Exception | None = None

        for model in models:
            try:
                response = retry_call(
                    lambda: client.models.generate_content(model=model, contents=prompt),
                    operation=f"Gemini QC ({model})",
                )
                response_text = response.text.strip()
                corrected = _parse_response_json(response_text)

                for lang in target_languages:
                    if lang not in corrected:
                        logger.warning(f"QC: language {lang} missing in response, using original")
                        corrected[lang] = translations.get(lang, "")

                corrected_json = json.dumps(corrected, ensure_ascii=False, indent=2)
                logger.info(f"Corrected translations JSON:\n{corrected_json}")
                logger.info(f"QC successful for {len(corrected)} languages using {model}")
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
