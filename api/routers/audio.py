"""The audio pipeline: an Excel sheet voiced into every target language.
Story jobs are polled and cancelled through these job endpoints too."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import openpyxl
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api import shared
from api.background import spawn
from api.utils import ensure_file_extension, parse_target_languages, save_upload_file
from batch.models import CreateJobResponse, JobState
from batch.service import english_voice_is_required, run_excel_batch_job
from services.runtime_config import MissingSettingError
from services.settings import Settings

router = APIRouter()


@router.post("/batch/excel-jobs", status_code=202, response_model=CreateJobResponse)
async def create_excel_job(
    file: UploadFile = File(...),
    target_languages: list[str] | None = Form(default=None),
    target_languages_json: str | None = Form(default=None),
    max_language_parallelism: int | None = Form(default=None),
    teaching_mode: bool | None = Form(default=False),
    mode: str = Form(default="create"),
):
    ensure_file_extension(file.filename, ".xlsx", "Only .xlsx files are allowed")
    if max_language_parallelism is not None and max_language_parallelism < 1:
        raise HTTPException(status_code=400, detail="max_language_parallelism must be >= 1")

    if mode not in {"create", "append"}:
        raise HTTPException(status_code=400, detail="mode must be 'create' or 'append'")

    parsed_languages = parse_target_languages(target_languages, target_languages_json)

    # Resolve everything the batch will need before accepting it. An Excel batch
    # runs for hours; a missing key should cost one HTTP response, not an hour.
    try:
        settings = Settings.resolve()
        english_voice_is_required(parsed_languages, settings.eleven)
    except (MissingSettingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    saved_path = Path(temp_file.name)
    temp_file.close()

    await save_upload_file(file, saved_path)

    job_id = uuid.uuid4().hex
    await shared.jobs_store.create(job_id)

    spawn(
        run_excel_batch_job(
            job_id=job_id,
            excel_path=str(saved_path),
            target_languages=parsed_languages,
            max_language_parallelism=max_language_parallelism,
            jobs_store=shared.jobs_store,
            settings=settings,
            teaching_mode=teaching_mode,
            output_dir=shared.OUTPUT_DIR,
            mode=mode,
        ),
        name=f"excel-batch:{job_id}",
    )

    return CreateJobResponse(job_id=job_id, status="queued")


@router.get("/batch/excel-jobs/{job_id}", response_model=JobState)
async def get_excel_job(job_id: str):
    job = await shared.jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/batch/excel-jobs/{job_id}/cancel")
async def cancel_excel_job(job_id: str):
    job = await shared.jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = await shared.jobs_store.request_cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, cannot cancel")
    return {"job_id": job_id, "status": "cancelling"}


@router.post("/batch/preview-excel")
async def preview_excel(file: UploadFile = File(...)):
    ensure_file_extension(file.filename, ".xlsx", "Only .xlsx files are allowed")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    saved = Path(tmp.name)
    tmp.close()
    await save_upload_file(file, saved)
    try:
        wb = openpyxl.load_workbook(str(saved), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows_out = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            cells = [str(c).strip() if c is not None else "" for c in row]
            rows_out.append(cells)
            if i >= 3:
                break
        wb.close()
        return {"rows": rows_out}
    finally:
        saved.unlink(missing_ok=True)
