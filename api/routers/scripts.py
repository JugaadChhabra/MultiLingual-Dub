"""The script writer: a day's drafts for the operator to read before any render."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, HTTPException

from api import shared
from services.runtime_config import MissingSettingError
from services.script_writer import ScriptRequestError, ScriptWriterError, ScriptWriterSettings

router = APIRouter()


@router.post("/video/scripts/generate")
async def generate_video_scripts(
    brief: str = Form(...),
    language: str = Form(default="hi-IN"),
    publish_date: str = Form(...),
    category: str = Form(default="default"),
    item_set: str = Form(default="zodiac"),
    title: str | None = Form(default=None),
):
    """Write the day's scripts and hand them back as drafts.

    Nothing is rendered here and no job is created: the response is text for an
    operator to read and edit. A render costs money at submission, so the two
    steps stay separate — see services/script_writer.py.

    Synchronous rather than a background job. One Gemini call for the whole set
    takes seconds, and a draft the operator is waiting to read has nothing to
    gain from a polling endpoint.
    """
    try:
        settings = ScriptWriterSettings.resolve()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Blocking throughout — history read, the Gemini call, history write — so
    # the whole day runs off the event loop. A set whose repair budget ran out
    # still comes back, with the unresolved clashes as warnings.
    try:
        day = await asyncio.to_thread(
            shared.script_writer.write_day,
            category=category,
            language=language,
            publish_date=publish_date,
            brief=brief,
            item_set=item_set,
            title=title,
            settings=settings,
        )
    except ScriptRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ScriptWriterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "language": language,
        "publish_date": publish_date,
        "items": [{"video_title": d.title, "script": d.script} for d in day.drafts],
        "warnings": day.warnings,
    }
