from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class ExcelRow(BaseModel):
    row_index: int
    text: str
    emotion: str = ""
    activity_name: str = ""
    audio_type: str = ""



class JobSummary(BaseModel):
    total_rows: int = 0
    rows_processed: int = 0
    rows_succeeded: int = 0
    rows_failed: int = 0
    language_tasks_total: int = 0
    language_tasks_succeeded: int = 0
    language_tasks_failed: int = 0
    uploads_succeeded: int = 0
    uploads_failed: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class JobState(BaseModel):
    job_id: str
    status: str = Field(pattern="^(queued|running|completed|failed)$")
    summary: JobSummary = Field(default_factory=JobSummary)
    error: str | None = None


class CreateJobResponse(BaseModel):
    job_id: str
    status: str
