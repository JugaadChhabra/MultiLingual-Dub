"""Story mode: split a pasted story into lines, export them, or voice them."""
from __future__ import annotations

import asyncio
import re
import uuid
from io import BytesIO

from fastapi import APIRouter, HTTPException, Response
from openpyxl import Workbook

from api import shared
from api.background import spawn
from api.models import StoryJobRequest, StorySplitRequest, StoryXlsxRequest
from api.utils import parse_target_languages
from batch.excel import REQUIRED_HEADERS
from batch.models import CreateJobResponse
from batch.service import english_voice_is_required
from batch.story_job import run_story_job
from services.qc import QCSettings
from services.runtime_config import MissingSettingError
from services.settings import Settings
from services.story import (
    MAX_LINE_CHARS,
    STORY_EMOTIONS,
    StoryGeminiError,
    StoryLine,
    StorySplitError,
    activity_name as story_activity_name,
    line_problem,
    split_story,
)

router = APIRouter()


@router.post("/batch/story/split")
async def split_story_route(payload: StorySplitRequest):
    """Break a pasted story into voiceover lines with emotions, for review.
    Synchronous: one Gemini call (two if the first split fails validation)."""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Paste a story first: the text is empty")
    try:
        settings = QCSettings.resolve()
    except (MissingSettingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        lines = await asyncio.to_thread(split_story, payload.text, settings=settings)
    except StorySplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StoryGeminiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "activity_name": story_activity_name(payload.title),
        "lines": [{"text": line.text, "emotion": line.emotion} for line in lines],
        # The review table is fed from here so it and the validator share one
        # list and one cap.
        "emotions": STORY_EMOTIONS,
        "max_line_chars": MAX_LINE_CHARS,
    }


@router.post("/batch/story/xlsx")
async def story_xlsx_route(payload: StoryXlsxRequest):
    """The reviewed lines as a sheet the normal Audio (Excel) flow accepts."""

    activity = payload.activity_name.strip()
    if not activity:
        raise HTTPException(status_code=400, detail="The activity name is empty")
    if not payload.lines:
        raise HTTPException(status_code=400, detail="There are no lines to download")
    for n, line in enumerate(payload.lines, start=1):
        if not line.text.strip():
            raise HTTPException(status_code=400, detail=f"Line {n} is empty")
        if line.emotion not in STORY_EMOTIONS:
            raise HTTPException(status_code=400, detail=f"Line {n} has an unknown emotion: {line.emotion}")

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(REQUIRED_HEADERS)
    for n, line in enumerate(payload.lines, start=1):
        sheet.append([line.text.strip(), line.emotion, activity, f"{activity}{n}"])
    buffer = BytesIO()
    workbook.save(buffer)
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", activity) + ".xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/batch/story-jobs", status_code=202, response_model=CreateJobResponse)
async def create_story_job(payload: StoryJobRequest):
    """Voice a reviewed story: translate, one story fix per language, speak.
    Polled and cancelled through the Excel job endpoints."""
    activity = payload.activity_name.strip()
    if not activity:
        raise HTTPException(status_code=400, detail="The activity name is empty")
    if not payload.lines:
        raise HTTPException(status_code=400, detail="There are no lines to voice")
    lines = [StoryLine(text=line.text.strip(), emotion=line.emotion) for line in payload.lines]
    for n, line in enumerate(lines, start=1):
        problem = line_problem(line)
        if problem:
            raise HTTPException(status_code=400, detail=f"Line {n}: {problem}")

    parsed_languages = parse_target_languages(payload.target_languages, None)

    try:
        settings = Settings.resolve()
        english_voice_is_required(parsed_languages, settings.eleven)
    except (MissingSettingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = uuid.uuid4().hex
    await shared.jobs_store.create(job_id)

    spawn(
        run_story_job(
            job_id=job_id,
            title=payload.title.strip(),
            activity_name=activity,
            lines=lines,
            target_languages=parsed_languages,
            jobs_store=shared.jobs_store,
            settings=settings,
            output_dir=shared.OUTPUT_DIR,
        ),
        name=f"story-job:{job_id}",
    )

    return CreateJobResponse(job_id=job_id, status="queued")
