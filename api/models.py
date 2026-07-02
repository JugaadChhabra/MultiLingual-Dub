from __future__ import annotations

from pydantic import BaseModel, Field


class TranslateRequest(BaseModel):
    text: str
    target_languages: list[str]


class FinalizeTextRequest(BaseModel):
    text: str
    language: str


class ElevenLabsTTSRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True
    speed: float = Field(default=1.0, ge=0.7, le=1.2)


class SessionEnvConfigRequest(BaseModel):
    env_text: str


class SessionEnvConfigResponse(BaseModel):
    configured: bool
    missing_keys: list[str]
