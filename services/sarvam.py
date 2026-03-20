from __future__ import annotations
from sarvamai import SarvamAI

from services.runtime_config import RuntimeConfig, get_config_value


def get_sarvam_client(runtime_config: RuntimeConfig | None = None) -> SarvamAI:
    api_key = get_config_value("SARVAM_API", runtime_config=runtime_config)
    if not api_key:
        raise ValueError("Missing SARVAM_API environment variable.")
    return SarvamAI(api_subscription_key=api_key)
