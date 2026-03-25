from __future__ import annotations

import os
from io import StringIO

from dotenv import dotenv_values

RuntimeConfig = dict[str, str]

REQUIRED_ENV_KEYS = [
    "SARVAM_API",
    "GEMINI_API_KEY",
    "WASABI_ACCESS_KEY",
    "WASABI_SECRET_KEY",
    "WASABI_BUCKET",
    "WASABI_REGION",
    "WASABI_ENDPOINT_URL",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "AWS_BUCKET",
    "AWS_REGION",
    "BATCH_ENABLE_WASABI_UPLOAD",
    "BATCH_ENABLE_QC",
    "ELEVEN_LABS",
    "AI_STUDIO_VOICE",
    "DESI_VOCAL_VOICE",
    "ENGLISH_VOICE",
]


def parse_env_text(env_text: str) -> RuntimeConfig:
    parsed = dotenv_values(stream=StringIO(env_text))
    result: RuntimeConfig = {}
    for key, value in parsed.items():
        if key is None:
            continue
        if value is None:
            continue
        result[str(key).strip()] = str(value).strip()
    return result


def get_missing_required_keys(config: RuntimeConfig) -> list[str]:
    missing: list[str] = []
    for key in REQUIRED_ENV_KEYS:
        value = config.get(key, "").strip()
        if not value:
            missing.append(key)
    return missing


def get_config_value(key: str, runtime_config: RuntimeConfig | None = None) -> str:
    if runtime_config is not None:
        value = runtime_config.get(key, "").strip()
        if value:
            return value
    return os.getenv(key, "").strip()


def get_effective_required_status(runtime_config: RuntimeConfig | None = None) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for key in REQUIRED_ENV_KEYS:
        value = get_config_value(key, runtime_config=runtime_config)
        if not value:
            missing.append(key)
    return (len(missing) == 0, missing)
