from __future__ import annotations

from pydantic import BaseModel


class TranslateRequest(BaseModel):
    text: str
    target_languages: list[str]


class FinalizeTextRequest(BaseModel):
    text: str
    language: str


class ElevenLabsTTSRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_v3"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True
