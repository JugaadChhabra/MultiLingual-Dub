from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import boto3
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
DEFAULT_QC_LOG_S3_PREFIX = "qc/"


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


def _get_qc_train_log_path() -> Path:
    raw_path = _get_qc_log_path()
    if raw_path.suffix:
        return raw_path.with_name(f"{raw_path.stem}-train{raw_path.suffix}")
    return raw_path.with_name(f"{raw_path.name}-train.jsonl")


def _get_qc_log_sink() -> str:
    raw = os.getenv("QC_LOG_SINK", "file").strip().lower()
    if raw in {"file", "s3"}:
        return raw
    logger.warning("QC: invalid QC_LOG_SINK=%s, defaulting to file", raw)
    return "file"


def _get_qc_s3_bucket() -> str | None:
    raw = os.getenv("QC_LOG_S3_BUCKET", "").strip()
    if raw:
        return raw
    raw = os.getenv("WASABI_BUCKET", "").strip()
    return raw or None


def _get_qc_s3_prefix() -> str:
    raw = os.getenv("QC_LOG_S3_PREFIX", DEFAULT_QC_LOG_S3_PREFIX).strip()
    if raw and not raw.endswith("/"):
        raw = raw + "/"
    return raw


def _get_qc_s3_endpoint() -> str | None:
    raw = os.getenv("QC_LOG_S3_ENDPOINT", "").strip()
    if raw:
        return raw
    raw = os.getenv("WASABI_ENDPOINT_URL", "").strip()
    return raw or None


def _get_qc_s3_region() -> str | None:
    raw = os.getenv("WASABI_REGION", "").strip()
    return raw or None


def _get_qc_s3_credentials() -> tuple[str, str] | None:
    access_key = os.getenv("WASABI_ACCESS_KEY", "").strip()
    secret_key = os.getenv("WASABI_SECRET_KEY", "").strip()
    if access_key or secret_key:
        if not access_key or not secret_key:
            logger.warning("QC: incomplete WASABI credentials; relying on default boto3 credential chain")
            return None
        return access_key, secret_key
    return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_train_log_enabled() -> bool:
    return _env_bool("QC_TRAIN_LOG_ENABLED", True)


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
) -> None:
    raw_path = _get_qc_log_path()
    raw_line = json.dumps(raw_payload, ensure_ascii=False)
    _write_jsonl_file(raw_path, [raw_line])

    if _is_train_log_enabled() and train_records:
        train_path = _get_qc_train_log_path()
        train_lines = [
            json.dumps(record, ensure_ascii=False) for record in train_records
        ]
        _write_jsonl_file(train_path, train_lines)


def _log_qc_sample_s3(
    *,
    raw_payload: dict[str, object],
    train_records: list[dict[str, object]],
    timestamp: datetime,
) -> None:
    bucket = _get_qc_s3_bucket()
    if not bucket:
        logger.warning("QC: QC_LOG_S3_BUCKET is missing; skipping S3 log")
        return
    prefix = _get_qc_s3_prefix()
    endpoint = _get_qc_s3_endpoint()
    region = _get_qc_s3_region()
    credentials = _get_qc_s3_credentials()
    client_kwargs: dict[str, object] = {"endpoint_url": endpoint, "region_name": region}
    if credentials:
        client_kwargs["aws_access_key_id"] = credentials[0]
        client_kwargs["aws_secret_access_key"] = credentials[1]
    client = boto3.client("s3", **client_kwargs)

    key_suffix = uuid4().hex
    raw_key = _build_s3_key(prefix, "raw", timestamp, key_suffix)
    raw_body = json.dumps(raw_payload, ensure_ascii=False) + "\n"
    client.put_object(Bucket=bucket, Key=raw_key, Body=raw_body.encode("utf-8"))

    if _is_train_log_enabled() and train_records:
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
    sink = _get_qc_log_sink()
    try:
        if sink == "s3":
            _log_qc_sample_s3(
                raw_payload=raw_payload,
                train_records=train_records,
                timestamp=now,
            )
        else:
            _log_qc_sample_file(
                raw_payload=raw_payload,
                train_records=train_records,
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
