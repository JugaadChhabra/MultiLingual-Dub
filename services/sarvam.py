from __future__ import annotations

from dataclasses import dataclass

from sarvamai import SarvamAI

from services.runtime_config import RuntimeConfig, require


@dataclass(frozen=True)
class SarvamSettings:
    api_key: str

    REQUIRED = ("SARVAM_API",)

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> SarvamSettings:
        return cls(api_key=require(cls.REQUIRED, session)["SARVAM_API"])


def get_sarvam_client(settings: SarvamSettings) -> SarvamAI:
    return SarvamAI(api_subscription_key=settings.api_key)
