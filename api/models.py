from __future__ import annotations

from datetime import datetime
from typing import Any

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


class SubtitleYoutubeRequest(BaseModel):
    youtube_url: str
    target_languages: list[str] = Field(default_factory=list)
    max_chars_per_translation_chunk: int = 1800


class CreateSubtitleJobResponse(BaseModel):
    job_id: str
    status: str


class SubtitleJobState(BaseModel):
    job_id: str
    status: str = Field(pattern="^(queued|running|completed|failed)$")
    result: dict[str, Any] | None = None
    error: str | None = None
    progress_step: str | None = None
    progress_message: str | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
