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


class SessionEnvConfigRequest(BaseModel):
    env_text: str


class SessionEnvConfigResponse(BaseModel):
    configured: bool
    missing_keys: list[str]
    # Every key the app requires, derived from the settings classes. Sent so the
    # UI can render an accurate count instead of maintaining its own list.
    required_keys: list[str] = []


class StorySplitRequest(BaseModel):
    title: str = ""
    text: str


class StoryLineIn(BaseModel):
    text: str
    emotion: str


class StoryXlsxRequest(BaseModel):
    activity_name: str
    lines: list[StoryLineIn]


class StoryJobRequest(BaseModel):
    activity_name: str
    lines: list[StoryLineIn]
    target_languages: list[str]
    # Context for the story fix only; nothing is named after it.
    title: str = ""
