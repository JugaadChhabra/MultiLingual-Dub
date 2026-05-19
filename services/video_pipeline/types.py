from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class VideoJobSpec(BaseModel):
    script: str
    voice_id: str | None = None
    model_id: str = "eleven_v3"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True

    video_prompt: str | None = None
    motion_prompt: str | None = None
    talking_photo_id: str | None = None
    width: int | None = None
    height: int | None = None
    video_title: str = "HeyGen Avatar IV Job"


class VideoJobSummary(BaseModel):
    audio_bytes: int = 0
    audio_path: str | None = None
    image_key: str | None = None
    audio_asset_id: str | None = None
    heygen_video_id: str | None = None
    video_url: str | None = None
    video_path: str | None = None
    nas_path: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class VideoJobState(BaseModel):
    job_id: str
    status: str = Field(pattern="^(queued|tts|uploading|generating|polling|downloading|nas_upload|completed|failed)$")
    stage_message: str = ""
    summary: VideoJobSummary = Field(default_factory=VideoJobSummary)
    error: str | None = None
