"""Rendering one video: the avatar list, the submission, its progress and cancel.
A batch of rows is api/routers/video_batch.py."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api import shared
from api.background import spawn
from api.utils import to_output_url
from services.runtime_config import MissingSettingError
from services.video_pipeline import run_video_job
from services.video_pipeline.intake import IntakeError, SingleSubmission, spec_from_single

router = APIRouter()


@router.get("/video/heygen/talking-photos")
async def list_heygen_talking_photos():
    try:
        photos = await shared.build_renderer().list_photos()
        return {"items": [
            {"talking_photo_id": p.talking_photo_id, "name": p.name, "preview_url": p.preview_url}
            for p in photos
        ]}
    except MissingSettingError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HeyGen list failed: {exc}") from exc


NO_PHOTO_DETAIL = "provide either an image file or a talking_photo_id"


@router.post("/video/heygen", status_code=202)
async def create_heygen_video_job(
    image: UploadFile | None = File(default=None),
    talking_photo_id: str | None = Form(default=None),
    script: str = Form(...),
    character: str = Form(default="indian"),
    voice_id: str | None = Form(default=None),
    motion_prompt: str | None = Form(default=None),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    # video_title becomes the NAS filename (DD-MM-YYYY/<title>.mp4), so a shared
    # default silently overwrites the previous render of the day. Callers should
    # always send a distinct title; the default stays only for compatibility.
    video_title: str = Form(default="HeyGen Avatar IV Job"),
    publish_date: str | None = Form(default=None),
    stability: float = Form(default=0.5),
    similarity_boost: float = Form(default=0.75),
    style: float = Form(default=0.0),
    # Toggles arrive as the raw strings the form sent; intake parses them.
    use_speaker_boost: str | None = Form(default=None),
    # Opt-outs for the branded overlay cards and the music bed (independent).
    # Batch takes the same two; only recovery has no knobs, and it replays the
    # job's saved spec.
    include_overlay: str | None = Form(default=None),
    include_music: str | None = Form(default=None),
):
    try:
        spec = spec_from_single(SingleSubmission(
            script=script,
            video_title=video_title,
            character=character,
            voice_id=voice_id,
            motion_prompt=motion_prompt,
            publish_date=publish_date,
            talking_photo_id=talking_photo_id,
            width=width,
            height=height,
            stability=stability,
            similarity_boost=similarity_boost,
            style=style,
            use_speaker_boost=use_speaker_boost,
            include_overlay=include_overlay,
            include_music=include_music,
        ))
    except IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Judged on the spec, so a whitespace-only id counts as none.
    if not (spec.talking_photo_id or (image and image.filename)):
        raise HTTPException(status_code=400, detail=NO_PHOTO_DETAIL)

    image_bytes = b""
    image_filename = "image.jpg"
    if image and image.filename:
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="image upload was empty")
        image_filename = image.filename

    try:
        deps = shared.build_video_deps()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = uuid.uuid4().hex
    await shared.video_jobs_store.create(job_id)

    control = shared.video_controls.open(job_id)
    spawn(
        shared.controlled(job_id, run_video_job(
            job_id=job_id,
            spec=spec,
            image_bytes=image_bytes,
            image_filename=image_filename,
            output_dir=shared.VIDEO_OUTPUT_DIR,
            jobs_store=shared.video_jobs_store,
            renderer=deps.renderer,
            speech=deps.speech,
            slots=deps.slots,
            nas_config=deps.nas,
            voice_id=deps.heygen.voice_for_character(spec.character),
            control=control,
        )),
        name=f"video-job:{job_id}",
    )

    return {"job_id": job_id, "status": "queued"}


@router.get("/video/heygen/{job_id}")
async def get_heygen_video_job(job_id: str):
    state = await shared.video_jobs_store.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = state.model_dump(mode="json")
    if state.summary.video_path:
        rel = to_output_url(state.summary.video_path, shared.OUTPUT_DIR)
        payload["video_local_url"] = rel
    control = shared.video_controls.get(job_id)
    payload["cancel_requested"] = bool(control and control.cancelled)
    return payload


@router.post("/video/heygen/{job_id}/cancel")
async def cancel_heygen_video_job(job_id: str):
    """Stop a single render at its next step. Once HeyGen has it, the render
    still finishes (and is billed) on their side, but it is not filed."""
    if not await shared.video_jobs_store.get(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    control = shared.video_controls.get(job_id)
    if control is None:
        raise HTTPException(status_code=409, detail="Job is not running, so there is nothing to cancel")
    control.cancel()
    return {"job_id": job_id, "status": "cancelling"}
