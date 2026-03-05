from __future__ import annotations
import os
from sarvamai import SarvamAI


def get_sarvam_client() -> SarvamAI:
    api_key = os.getenv("SARVAM_API", "").strip()
    if not api_key:
        raise ValueError("Missing SARVAM_API environment variable.")
    return SarvamAI(api_subscription_key=api_key)
