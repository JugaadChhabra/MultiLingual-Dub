from __future__ import annotations
import json
import logging
from dataclasses import dataclass

import google.genai as genai
from google.genai import types

from services.languages import LANGUAGE_NAMES, LANGUAGE_SCRIPT_HINTS
from services.retry import retry_call
from services.runtime_config import RuntimeConfig, read_setting, require

logger = logging.getLogger(__name__)

DEFAULT_QC_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]

_TRUTHY = {"1", "true", "yes", "on"}


class QCError(Exception):
    pass


@dataclass(frozen=True)
class QCSettings:
    api_key: str
    models: list[str]
    # Audio is only ever generated after Gemini QC, so this being off is a
    # configuration error rather than a feature toggle. Validated on resolve so
    # a batch is rejected at submit instead of failing once it starts.
    enabled: bool

    REQUIRED = ("GEMINI_API_KEY", "BATCH_ENABLE_QC")

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> QCSettings:
        values = require(cls.REQUIRED, session)
        enabled = values["BATCH_ENABLE_QC"].strip().lower() in _TRUTHY
        if not enabled:
            raise ValueError(
                "BATCH_ENABLE_QC must be true: audio is generated only after Gemini QC"
            )
        raw = read_setting("GEMINI_QC_MODELS", session).strip()
        models = [item.strip() for item in raw.split(",") if item.strip()] or DEFAULT_QC_MODELS
        return cls(api_key=values["GEMINI_API_KEY"], models=models, enabled=enabled)


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
    settings: QCSettings,
    teaching_mode: bool = False,
    thinking_budget: int | None = None,
) -> dict[str, str]:
    """
    QC multiple translations at once using Gemini.

    :param original_text: English source text
    :param translations: Dict of {language_code: translated_text}
    :param target_languages: List of language codes
    :param settings: Resolved Gemini configuration, from the edge of the request
    :param thinking_budget: Optional Gemini thinking-token budget. When None
        (default) the model's default thinking behaviour is used unchanged.
        Pass 0 to disable thinking. Used by eval tooling to A/B the setting.
    :return: Dict of {language_code: corrected_text}
    """
    if not target_languages or not translations:
        return translations

    try:
        client = genai.Client(api_key=settings.api_key)
        models = settings.models

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

        # Stable instructions go in the system prompt so they form a cacheable
        # prefix across every row in a batch (same target languages). Only the
        # per-row data below changes between requests. This is byte-for-byte the
        # same information the model saw before, just reorganised.
        system_instruction = f"""You are a translation quality-control expert for: {lang_descs}.

Script reference by language:
{script_descs}

Fix the translations and return corrected JSON using exactly the same keys as input.
{teaching_instructions}

Return only the corrected JSON object."""

        prompt = f"""Original English text:
"{original_text}"

Candidate translations JSON:
{translations_json}"""

        config = types.GenerateContentConfig(system_instruction=system_instruction)
        if thinking_budget is not None:
            config.thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)

        last_exc: Exception | None = None

        for model in models:
            try:
                response = retry_call(
                    lambda: client.models.generate_content(
                        model=model, contents=prompt, config=config
                    ),
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
