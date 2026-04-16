from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import boto3
import google.genai as genai

from services.retry import retry_call
from services.runtime_config import RuntimeConfig, get_config_value

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "bn-IN": "Bengali",
    "de": "German",
    "en-IN": "English",
    "es": "Spanish",
    "fr": "French",
    "gu-IN": "Gujarati",
    "hi-IN": "Hindi",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "od-IN": "Odia",
    "pa-IN": "Punjabi",
    "pt": "Portuguese",
    "ru": "Russian",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}

LANGUAGE_SCRIPT_HINTS = {
    "bn-IN": "Bengali script",
    "de": "Latin script (German)",
    "en-IN": "Latin script (English)",
    "es": "Latin script (Spanish)",
    "fr": "Latin script (French)",
    "gu-IN": "Gujarati script",
    "hi-IN": "Devanagari script",
    "kn-IN": "Kannada script",
    "ml-IN": "Malayalam script",
    "mr-IN": "Devanagari script",
    "od-IN": "Odia script",
    "pa-IN": "Gurmukhi script",
    "pt": "Latin script (Portuguese)",
    "ru": "Cyrillic script (Russian)",
    "ta-IN": "Tamil script",
    "te-IN": "Telugu script",
}

DEFAULT_QC_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]
DEFAULT_QC_LOG_S3_PREFIX = "qc/"


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


def _get_qc_log_path(runtime_config: RuntimeConfig | None = None) -> Path:
    raw = _cfg("QC_LOG_PATH", runtime_config=runtime_config, default="data/qc/qc-log.jsonl").strip()
    return Path(raw)


def _get_qc_train_log_path(runtime_config: RuntimeConfig | None = None) -> Path:
    raw_path = _get_qc_log_path(runtime_config=runtime_config)
    if raw_path.suffix:
        return raw_path.with_name(f"{raw_path.stem}-train{raw_path.suffix}")
    return raw_path.with_name(f"{raw_path.name}-train.jsonl")


def _get_qc_log_sink(runtime_config: RuntimeConfig | None = None) -> str:
    raw = _cfg("QC_LOG_SINK", runtime_config=runtime_config, default="s3").strip().lower()
    if raw in {"file", "s3"}:
        return raw
    logger.warning("QC: invalid QC_LOG_SINK=%s, defaulting to file", raw)
    return "file"


def _get_qc_s3_bucket(runtime_config: RuntimeConfig | None = None) -> str | None:
    raw = _cfg("WASABI_BUCKET", runtime_config=runtime_config).strip()
    return raw or None


def _get_qc_s3_prefix(runtime_config: RuntimeConfig | None = None) -> str:
    raw = _cfg("QC_LOG_S3_PREFIX", runtime_config=runtime_config, default=DEFAULT_QC_LOG_S3_PREFIX).strip()
    if raw and not raw.endswith("/"):
        raw = raw + "/"
    return raw


def _get_qc_s3_endpoint(runtime_config: RuntimeConfig | None = None) -> str | None:
    raw = _cfg("WASABI_ENDPOINT_URL", runtime_config=runtime_config).strip()
    return raw or None


def _get_qc_s3_region(runtime_config: RuntimeConfig | None = None) -> str | None:
    raw = _cfg("WASABI_REGION", runtime_config=runtime_config).strip()
    return raw or None


def _get_qc_s3_credentials(runtime_config: RuntimeConfig | None = None) -> tuple[str, str] | None:
    access_key = _cfg("WASABI_ACCESS_KEY", runtime_config=runtime_config).strip()
    secret_key = _cfg("WASABI_SECRET_KEY", runtime_config=runtime_config).strip()
    if access_key or secret_key:
        if not access_key or not secret_key:
            logger.warning("QC: incomplete WASABI credentials; relying on default boto3 credential chain")
            return None
        return access_key, secret_key
    return None


def _env_bool(name: str, default: bool, runtime_config: RuntimeConfig | None = None) -> bool:
    raw = _cfg(name, runtime_config=runtime_config)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_train_log_enabled(runtime_config: RuntimeConfig | None = None) -> bool:
    return _env_bool("QC_TRAIN_LOG_ENABLED", True, runtime_config=runtime_config)


def _build_raw_payload(
    *,
    timestamp: str,
    model: str,
    original_text: str,
    input_translations: dict[str, str],
    output_translations: dict[str, str],
    target_languages: list[str],
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": timestamp,
        "model": model,
        "original_text": original_text,
        "input_translations": input_translations,
        "output_translations": output_translations,
        "target_languages": target_languages,
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def _build_training_records(
    *,
    timestamp: str,
    model: str,
    original_text: str,
    input_translations: dict[str, str],
    output_translations: dict[str, str],
    target_languages: list[str],
    metadata: dict[str, object] | None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for lang_code in target_languages:
        input_text = input_translations.get(lang_code, "")
        output_text = output_translations.get(lang_code, "")
        language_name = LANGUAGE_NAMES.get(lang_code, lang_code)
        record: dict[str, object] = {
            "messages": [
                {
                    "role": "system",
                    "content": f"You are a QC expert for {language_name}.",
                },
                {
                    "role": "user",
                    "content": (
                        f'Original English: "{original_text}"\n'
                        f'Translation ({lang_code}): "{input_text}"\n'
                        "Fix any errors in the translation and return only the corrected translation."
                    ),
                },
                {"role": "assistant", "content": output_text},
            ],
            "lang_code": lang_code,
            "timestamp": timestamp,
            "model": model,
        }
        if metadata:
            record["metadata"] = metadata
        records.append(record)
    return records


def _write_jsonl_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _build_s3_key(prefix: str, stream: str, timestamp: datetime, key_suffix: str) -> str:
    date_part = timestamp.strftime("%Y/%m/%d")
    time_part = timestamp.strftime("%H%M%S")
    return f"{prefix}{stream}/{date_part}/{time_part}-{key_suffix}.jsonl"


def _log_qc_sample_file(
    *,
    raw_payload: dict[str, object],
    train_records: list[dict[str, object]],
    runtime_config: RuntimeConfig | None = None,
) -> None:
    raw_path = _get_qc_log_path(runtime_config=runtime_config)
    raw_line = json.dumps(raw_payload, ensure_ascii=False)
    _write_jsonl_file(raw_path, [raw_line])

    if _is_train_log_enabled(runtime_config=runtime_config) and train_records:
        train_path = _get_qc_train_log_path(runtime_config=runtime_config)
        train_lines = [
            json.dumps(record, ensure_ascii=False) for record in train_records
        ]
        _write_jsonl_file(train_path, train_lines)


def _log_qc_sample_s3(
    *,
    raw_payload: dict[str, object],
    train_records: list[dict[str, object]],
    timestamp: datetime,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    bucket = _get_qc_s3_bucket(runtime_config=runtime_config)
    if not bucket:
        logger.warning("QC: WASABI_BUCKET is missing; skipping S3 log")
        return
    prefix = _get_qc_s3_prefix(runtime_config=runtime_config)
    endpoint = _get_qc_s3_endpoint(runtime_config=runtime_config)
    region = _get_qc_s3_region(runtime_config=runtime_config)
    credentials = _get_qc_s3_credentials(runtime_config=runtime_config)
    client_kwargs: dict[str, object] = {"endpoint_url": endpoint, "region_name": region}
    if credentials:
        client_kwargs["aws_access_key_id"] = credentials[0]
        client_kwargs["aws_secret_access_key"] = credentials[1]
    client = boto3.client("s3", **client_kwargs)

    key_suffix = uuid4().hex
    raw_key = _build_s3_key(prefix, "raw", timestamp, key_suffix)
    raw_body = json.dumps(raw_payload, ensure_ascii=False) + "\n"
    client.put_object(Bucket=bucket, Key=raw_key, Body=raw_body.encode("utf-8"))

    if _is_train_log_enabled(runtime_config=runtime_config) and train_records:
        train_key = _build_s3_key(prefix, "train", timestamp, key_suffix)
        train_body = "\n".join(
            json.dumps(record, ensure_ascii=False) for record in train_records
        )
        client.put_object(
            Bucket=bucket,
            Key=train_key,
            Body=(train_body + "\n").encode("utf-8"),
        )


def _log_qc_sample(
    *,
    model: str,
    original_text: str,
    input_translations: dict[str, str],
    output_translations: dict[str, str],
    target_languages: list[str],
    metadata: dict[str, object] | None,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    now = datetime.utcnow()
    timestamp = now.isoformat() + "Z"
    raw_payload = _build_raw_payload(
        timestamp=timestamp,
        model=model,
        original_text=original_text,
        input_translations=input_translations,
        output_translations=output_translations,
        target_languages=target_languages,
        metadata=metadata,
    )
    train_records = _build_training_records(
        timestamp=timestamp,
        model=model,
        original_text=original_text,
        input_translations=input_translations,
        output_translations=output_translations,
        target_languages=target_languages,
        metadata=metadata,
    )
    sink = _get_qc_log_sink(runtime_config=runtime_config)
    try:
        if sink == "s3":
            _log_qc_sample_s3(
                raw_payload=raw_payload,
                train_records=train_records,
                timestamp=now,
                runtime_config=runtime_config,
            )
        else:
            _log_qc_sample_file(
                raw_payload=raw_payload,
                train_records=train_records,
                runtime_config=runtime_config,
            )
    except OSError as exc:
        logger.warning("QC: failed to write log sample: %s", exc)
    except Exception as exc:
        logger.warning("QC: failed to write log sample to %s sink: %s", sink, exc)


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
    runtime_config: RuntimeConfig | None = None,
    teaching_mode: bool = False,
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
        api_key = get_gemini_api_key(runtime_config=runtime_config)
        client = genai.Client(api_key=api_key)
        models = _get_qc_models(runtime_config=runtime_config)
        
        # Build language descriptions
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
        
        # Build JSON input
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
                    runtime_config=runtime_config,
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
