"""Rendering a batch: rows from a spreadsheet or from reviewed drafts, all
against one avatar, plus pause / resume / cancel while it runs."""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api import shared
from api.background import spawn
from api.routers.video import NO_PHOTO_DETAIL
from api.utils import ensure_file_extension, save_upload_file
from services.runtime_config import MissingSettingError
from services.script_writer import UnknownItemTitleError
from services.video_pipeline.batch_excel import (
    BatchExcelError,
    read_heygen_batch_rows,
    read_heygen_batch_rows_json,
)
from services.video_pipeline.batch_runner import run_video_batch_job
from services.video_pipeline.batch_store import BatchRowState
from services.video_pipeline.intake import BatchSettings, IntakeError, specs_from_batch

router = APIRouter()


@router.post("/video/heygen/batch", status_code=202)
async def create_heygen_batch_job(
    # Either an uploaded image or a photo picked from the account, as for a
    # single video.
    image: UploadFile | None = File(default=None),
    talking_photo_id: str | None = Form(default=None),
    excel: UploadFile | None = File(default=None),
    rows: str | None = Form(default=None),
    character: str = Form(default="indian"),
    motion_prompt: str | None = Form(default=None),
    publish_date: str | None = Form(default=None),
    # Only sent when the rows were written by the script writer. They identify
    # which history these rows belong to, so what is submitted can replace what
    # was generated — see the recording step below.
    category: str | None = Form(default=None),
    language: str | None = Form(default=None),
    # Strings, not bools: intake parses them, as for a single video.
    include_overlay: str | None = Form(default=None),
    include_music: str | None = Form(default=None),
):
    # Two ways in, one batch: a spreadsheet the operator authored, or rows of
    # generated scripts they reviewed in the browser. Both become the same row
    # list before anything else happens, so nothing downstream knows which.
    has_excel = bool(excel and excel.filename)
    has_rows = bool(rows and rows.strip())
    if has_excel == has_rows:
        raise HTTPException(
            status_code=400, detail="provide either an excel file or rows JSON, not both"
        )
    if has_excel:
        ensure_file_extension(excel.filename, ".xlsx", "Only .xlsx files are allowed")
        excel_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        excel_path = Path(excel_tmp.name)
        excel_tmp.close()
        await save_upload_file(excel, excel_path)
        try:
            batch_rows_parsed = await asyncio.to_thread(read_heygen_batch_rows, excel_path)
        except BatchExcelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            excel_path.unlink(missing_ok=True)
        empty_detail = "Excel has no non-empty script rows"
    else:
        try:
            batch_rows_parsed = read_heygen_batch_rows_json(rows or "")
        except BatchExcelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        empty_detail = "No non-empty scripts were submitted"

    if not batch_rows_parsed:
        raise HTTPException(status_code=400, detail=empty_detail)

    # Before history is recorded, so a batch refused here leaves no trace there.
    try:
        items = specs_from_batch(batch_rows_parsed, BatchSettings(
            character=character,
            motion_prompt=motion_prompt,
            publish_date=publish_date,
            talking_photo_id=talking_photo_id,
            include_overlay=include_overlay,
            include_music=include_music,
        ))
    except IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Intake cleaned the id (blank to None) and put it on every row alike.
    picked_photo_id = items[0].spec.talking_photo_id
    if not (picked_photo_id or (image and image.filename)):
        raise HTTPException(status_code=400, detail=NO_PHOTO_DETAIL)

    image_bytes = b""
    image_filename = "image.jpg"
    if not picked_photo_id:
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="image upload was empty")
        image_filename = image.filename

    # Record what is actually being rendered, replacing the drafts recorded when
    # they were written — the operator's edits are what tomorrow must avoid.
    # Only for generated rows: a spreadsheet carries no category or language to
    # file it under, and its scripts were not written by the model that would be
    # told to avoid them.
    if has_rows:
        try:
            await asyncio.to_thread(
                shared.script_writer.record_submitted,
                category=category,
                language=language,
                publish_date=publish_date,
                rows=[(item.spec.video_title, item.spec.script) for item in items],
            )
        except UnknownItemTitleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        deps = shared.build_video_deps()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch_id = uuid.uuid4().hex
    batch_rows = [
        BatchRowState(row_index=item.row_index, script=item.spec.script, video_title=item.spec.video_title)
        for item in items
    ]
    await shared.video_batch_jobs_store.create(batch_id, batch_rows)

    control = shared.video_controls.open(batch_id)
    spawn(
        shared.controlled(batch_id, run_video_batch_job(
            batch_id=batch_id,
            items=items,
            image_bytes=image_bytes,
            image_filename=image_filename,
            talking_photo_id=picked_photo_id,
            output_dir=shared.VIDEO_OUTPUT_DIR,
            output_base_dir=shared.OUTPUT_DIR,
            batch_store=shared.video_batch_jobs_store,
            video_jobs_store=shared.video_jobs_store,
            renderer=deps.renderer,
            speech=deps.speech,
            slots=deps.slots,
            heygen=deps.heygen,
            nas_config=deps.nas,
            email=deps.email,
            control=control,
        )),
        name=f"video-batch:{batch_id}",
    )

    return {"batch_id": batch_id, "status": "queued", "total": len(items)}


@router.get("/video/heygen/batch/{batch_id}")
async def get_heygen_batch_job(batch_id: str):
    state = await shared.video_batch_jobs_store.get(batch_id)
    if not state:
        raise HTTPException(status_code=404, detail="Batch job not found")
    control = shared.video_controls.get(batch_id)
    if control is not None:
        state.paused = control.paused
        state.cancel_requested = control.cancelled
    return state.model_dump(mode="json")


async def _batch_control(batch_id: str):
    if not await shared.video_batch_jobs_store.get(batch_id):
        raise HTTPException(status_code=404, detail="Batch job not found")
    control = shared.video_controls.get(batch_id)
    if control is None:
        raise HTTPException(status_code=409, detail="Batch is not running")
    return control


@router.post("/video/heygen/batch/{batch_id}/pause")
async def pause_heygen_batch(batch_id: str):
    """Start no new row, and hold rows HeyGen does not have yet. Renders already
    submitted keep going: they are paid for."""
    control = await _batch_control(batch_id)
    if control.cancelled:
        raise HTTPException(status_code=409, detail="Batch is already being cancelled")
    control.pause()
    return {"batch_id": batch_id, "paused": True}


@router.post("/video/heygen/batch/{batch_id}/resume")
async def resume_heygen_batch(batch_id: str):
    control = await _batch_control(batch_id)
    control.resume()
    return {"batch_id": batch_id, "paused": False}


@router.post("/video/heygen/batch/{batch_id}/cancel")
async def cancel_heygen_batch(batch_id: str):
    """Stop every row that has not finished. Rows already on the NAS stay."""
    control = await _batch_control(batch_id)
    control.cancel()
    return {"batch_id": batch_id, "status": "cancelling"}
