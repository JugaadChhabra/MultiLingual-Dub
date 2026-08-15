from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import uuid
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.background import spawn
from api.logging import get_important_logs as _get_important_logs, install_log_handler
from api.models import (
    ElevenLabsTTSRequest,
    FinalizeTextRequest,
    SessionEnvConfigRequest,
    SessionEnvConfigResponse,
    TranslateRequest,
)
from api.session_config import SESSION_COOKIE_NAME, SessionConfigStore
from api.utils import ensure_file_extension, parse_target_languages, safe_stem, save_upload_file, to_output_url
from batch.models import CreateJobResponse, JobState
from batch.service import english_voice_is_required, run_excel_batch_job
from services.elevenlabs import ElevenLabsSettings, ElevenLabsTTSConfig, synthesize_speech_bytes
from batch.store import JobsStore
from services.video_pipeline import (
    VideoJobSpec,
    VideoJobsStore,
    recover_video_job,
    run_video_job,
)
from services.email import EmailSettings
from services.nas import NasConfig, NasService
from services.video_pipeline.batch_excel import BatchExcelError, read_heygen_batch_rows
from services.video_pipeline.batch_runner import run_video_batch_job
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.heygen_client import HeyGenSettings, list_talking_photos
from services.video_pipeline.heygen_renderer import HeyGenRenderer
from services.video_pipeline.renderer import VideoRenderer
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.speech import ElevenLabsSpeech, SpeechSynth
from services.sarvam import SarvamSettings
from services.settings import Settings, missing_keys, required_keys
from services.stt import transcribe_audio
from services.tts import text_to_speech
from services.translation import translate_with_fallback
from services.s3 import validate_s3_env
from services.runtime_config import MissingSettingError, parse_env_text, RuntimeConfig


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
install_log_handler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _, s3_error = validate_s3_env()
    app.state.s3_config_error = s3_error
    try:
        NasService(NasConfig.resolve()).ensure_base_folders()
    except Exception as exc:
        logging.warning("NAS base folder init failed (non-fatal): %s", exc)
    yield


app = FastAPI(lifespan=lifespan)

UPLOAD_DIR = Path("./uploads")
OUTPUT_DIR = Path("./output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

STATIC_DIR = Path("./static")
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# All three stores mirror to disk so a restart leaves a record rather than a
# 404. Only video jobs can actually be resumed from theirs — a HeyGen render
# outlives the process — but knowing how far a batch got is worth the file.
jobs_store = JobsStore(persist_dir=OUTPUT_DIR / "batch" / "_jobs")
video_jobs_store = VideoJobsStore(persist_dir=OUTPUT_DIR / "heygen" / "_jobs")
video_batch_jobs_store = VideoBatchJobsStore(persist_dir=OUTPUT_DIR / "heygen" / "_batches")
session_config_store = SessionConfigStore()

VIDEO_OUTPUT_DIR = OUTPUT_DIR / "heygen"
VIDEO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


async def _session_config_for_request(request: Request) -> RuntimeConfig | None:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return await session_config_store.get(session_id)


@dataclass(frozen=True)
class VideoDeps:
    """Everything a video job runs against, resolved at the edge."""

    renderer: VideoRenderer
    speech: SpeechSynth
    slots: TalkingPhotoSlots
    heygen: HeyGenSettings
    nas: NasConfig
    email: EmailSettings


def _build_video_deps(session: RuntimeConfig | None) -> VideoDeps:
    """Resolve credentials and settings once, here, and build the seams a video
    job runs against.

    A missing key raises here — so it fails the request that asked for the work,
    instead of failing a background job several minutes in.
    """
    heygen = HeyGenSettings.resolve(session)
    eleven = ElevenLabsSettings.resolve(session)
    renderer = HeyGenRenderer(heygen.api_key)
    return VideoDeps(
        renderer=renderer,
        speech=ElevenLabsSpeech(eleven.api_key),
        slots=TalkingPhotoSlots(renderer),
        heygen=heygen,
        nas=NasConfig.resolve(session),
        email=EmailSettings.resolve(session),
    )


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
    )


# The HTML must always be revalidated. Without an explicit Cache-Control browsers
# apply heuristic freshness and can serve a stale page against freshly deployed
# JS — the markup then lacks the ids the new script expects and the page dies on
# load. The ?v= query on the asset tags only helps if the HTML carrying it is
# current, so the pages say no-cache and /static assets stay cacheable.
NO_HTML_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def index() -> FileResponse:
    index_path = Path("./index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path, headers=NO_HTML_CACHE)


@app.get("/videogen")
def videogen_page() -> FileResponse:
    """Both pipelines live in one app now. This stays a real route so the Video
    section can be linked and bookmarked directly — the shell reads the path and
    opens it."""
    return index()


@app.get("/heygen", include_in_schema=False)
def heygen_page_legacy() -> RedirectResponse:
    """The old name. Kept as a redirect rather than deleted: the video editor has
    this bookmarked, and a 308 renames the URL without costing them anything."""
    return RedirectResponse("/videogen", status_code=308)


@app.get("/video/heygen/talking-photos")
async def list_heygen_talking_photos(request: Request):
    session = await _session_config_for_request(request)
    try:
        heygen = HeyGenSettings.resolve(session)
        return {"items": await asyncio.to_thread(list_talking_photos, api_key=heygen.api_key)}
    except MissingSettingError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HeyGen list failed: {exc}") from exc


@app.post("/video/heygen", status_code=202)
async def create_heygen_video_job(
    request: Request,
    image: UploadFile | None = File(default=None),
    talking_photo_id: str | None = Form(default=None),
    script: str = Form(...),
    character: str = Form(default="indian"),
    voice_id: str | None = Form(default=None),
    video_prompt: str | None = Form(default=None),
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
    use_speaker_boost: bool = Form(default=True),
):
    session = await _session_config_for_request(request)

    if not script.strip():
        raise HTTPException(status_code=400, detail="script must not be empty")

    if not (talking_photo_id or (image and image.filename)):
        raise HTTPException(status_code=400, detail="provide either an image file or a talking_photo_id")

    image_bytes = b""
    image_filename = "image.jpg"
    if image and image.filename:
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="image upload was empty")
        image_filename = image.filename

    spec = VideoJobSpec(
        script=script,
        character=character or "indian",
        voice_id=voice_id or None,
        video_prompt=video_prompt or None,
        motion_prompt=motion_prompt or None,
        width=width,
        height=height,
        video_title=video_title,
        publish_date=publish_date or None,
        stability=stability,
        similarity_boost=similarity_boost,
        style=style,
        use_speaker_boost=use_speaker_boost,
        talking_photo_id=talking_photo_id or None,
    )

    try:
        deps = _build_video_deps(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = uuid.uuid4().hex
    await video_jobs_store.create(job_id)

    spawn(
        run_video_job(
            job_id=job_id,
            spec=spec,
            image_bytes=image_bytes,
            image_filename=image_filename,
            output_dir=VIDEO_OUTPUT_DIR,
            jobs_store=video_jobs_store,
            renderer=deps.renderer,
            speech=deps.speech,
            slots=deps.slots,
            nas_config=deps.nas,
            voice_id=deps.heygen.voice_for_character(character),
        ),
        name=f"video-job:{job_id}",
    )

    return {"job_id": job_id, "status": "queued"}


@app.get("/video/heygen/{job_id}")
async def get_heygen_video_job(job_id: str):
    state = await video_jobs_store.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = state.model_dump(mode="json")
    if state.summary.video_path:
        rel = to_output_url(state.summary.video_path, OUTPUT_DIR)
        payload["video_local_url"] = rel
    return payload


@app.get("/video/heygen/jobs/recoverable")
async def list_recoverable_heygen_jobs():
    """Failed jobs whose HeyGen render actually finished — re-runnable via recover."""
    ids = await video_jobs_store.list_recoverable()
    return {"recoverable": ids, "count": len(ids)}


@app.post("/video/heygen/{job_id}/recover", status_code=202)
async def recover_heygen_video_job(job_id: str, request: Request):
    """Re-download a finished-but-failed render and push it to the NAS. Recovers
    a job that died on the download/NAS step (its HeyGen render is intact)."""
    session = await _session_config_for_request(request)
    state = await video_jobs_store.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    if not state.summary.heygen_video_id:
        raise HTTPException(status_code=409, detail="Job has no HeyGen render to recover")
    if not state.spec:
        raise HTTPException(status_code=409, detail="Job has no persisted spec to recover from")

    try:
        deps = _build_video_deps(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    spawn(
        recover_video_job(
            job_id=job_id,
            jobs_store=video_jobs_store,
            output_dir=VIDEO_OUTPUT_DIR,
            renderer=deps.renderer,
            nas_config=deps.nas,
        ),
        name=f"video-recover:{job_id}",
    )
    return {"job_id": job_id, "status": "recovering"}


@app.post("/video/heygen/recover-failed", status_code=202)
async def recover_all_failed_heygen_jobs(request: Request):
    """Recover every failed job whose render finished on HeyGen, in one shot."""
    session = await _session_config_for_request(request)
    try:
        deps = _build_video_deps(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ids = await video_jobs_store.list_recoverable()
    for jid in ids:
        spawn(
            recover_video_job(
                job_id=jid,
                jobs_store=video_jobs_store,
                output_dir=VIDEO_OUTPUT_DIR,
                renderer=deps.renderer,
                nas_config=deps.nas,
            ),
            name=f"video-recover:{jid}",
        )
    return {"status": "recovering", "job_ids": ids, "count": len(ids)}


@app.post("/video/heygen/batch", status_code=202)
async def create_heygen_batch_job(
    request: Request,
    image: UploadFile = File(...),
    excel: UploadFile = File(...),
    character: str = Form(default="indian"),
    video_prompt: str | None = Form(default=None),
    motion_prompt: str | None = Form(default=None),
    publish_date: str | None = Form(default=None),
):
    session = await _session_config_for_request(request)

    ensure_file_extension(excel.filename, ".xlsx", "Only .xlsx files are allowed")
    if not (image and image.filename):
        raise HTTPException(status_code=400, detail="image file is required")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="image upload was empty")

    excel_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    excel_path = Path(excel_tmp.name)
    excel_tmp.close()
    await save_upload_file(excel, excel_path)

    try:
        rows = await asyncio.to_thread(read_heygen_batch_rows, excel_path)
    except BatchExcelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        excel_path.unlink(missing_ok=True)

    if not rows:
        raise HTTPException(status_code=400, detail="Excel has no non-empty script rows")

    try:
        deps = _build_video_deps(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch_id = uuid.uuid4().hex
    batch_rows = [
        BatchRowState(row_index=r.row_index, script=r.script, video_title=r.video_title)
        for r in rows
    ]
    await video_batch_jobs_store.create(batch_id, batch_rows)

    spawn(
        run_video_batch_job(
            batch_id=batch_id,
            rows=rows,
            image_bytes=image_bytes,
            image_filename=image.filename,
            character=character or "indian",
            video_prompt=video_prompt or None,
            motion_prompt=motion_prompt or None,
            publish_date=publish_date or None,
            output_dir=VIDEO_OUTPUT_DIR,
            output_base_dir=OUTPUT_DIR,
            batch_store=video_batch_jobs_store,
            video_jobs_store=video_jobs_store,
            renderer=deps.renderer,
            speech=deps.speech,
            slots=deps.slots,
            heygen=deps.heygen,
            nas_config=deps.nas,
            email=deps.email,
        ),
        name=f"video-batch:{batch_id}",
    )

    return {"batch_id": batch_id, "status": "queued", "total": len(rows)}


@app.get("/video/heygen/batch/{batch_id}")
async def get_heygen_batch_job(batch_id: str):
    state = await video_batch_jobs_store.get(batch_id)
    if not state:
        raise HTTPException(status_code=404, detail="Batch job not found")
    return state.model_dump(mode="json")


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint for Docker healthchecks and monitoring."""
    return {"status": "healthy", "service": "autodub"}


@app.post("/config/session-env", response_model=SessionEnvConfigResponse)
async def set_session_env_config(payload: SessionEnvConfigRequest, request: Request):
    parsed = parse_env_text(payload.env_text)
    # Required keys are derived from the settings classes, so this check can no
    # longer drift from what the app actually reads.
    absent = [key for key in required_keys() if not parsed.get(key, "").strip()]
    if absent:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Missing required keys in pasted .env text",
                "missing_keys": absent,
            },
        )

    session_id = request.cookies.get(SESSION_COOKIE_NAME) or session_config_store.generate_session_id()
    await session_config_store.set(session_id, parsed)

    response = JSONResponse(
        status_code=200,
        content=SessionEnvConfigResponse(
            configured=True, missing_keys=[], required_keys=required_keys()
        ).model_dump(),
    )
    _set_session_cookie(response, session_id)
    return response


@app.get("/config/session-env/status", response_model=SessionEnvConfigResponse)
async def get_session_env_config_status(request: Request):
    session = await _session_config_for_request(request)
    absent = missing_keys(session)
    return SessionEnvConfigResponse(
        configured=not absent, missing_keys=absent, required_keys=required_keys()
    )


@app.delete("/config/session-env", response_model=SessionEnvConfigResponse)
async def clear_session_env_config(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        await session_config_store.clear(session_id)
    response = JSONResponse(
        status_code=200,
        content=SessionEnvConfigResponse(
            configured=False, missing_keys=required_keys(), required_keys=required_keys()
        ).model_dump(),
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/logs/important")
async def get_important_logs(since_id: int = 0, limit: int = 200) -> dict:
    return _get_important_logs(since_id, limit)


@app.post("/translate")
async def translate_pipeline(payload: TranslateRequest, request: Request):
    session = await _session_config_for_request(request)
    try:
        sarvam = SarvamSettings.resolve(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    transcript = payload.text
    translations = {}
    for lang in payload.target_languages:
        try:
            translated = translate_with_fallback(
                transcript,
                settings=sarvam,
                target_language_code=lang,
                source_language_code="auto",
            )
        except Exception as exc:
            translated = f"[Translation error: {exc}]"
        translations[lang] = translated

    return {
        "input_text": transcript,
        "translations": translations,
    }


@app.post("/batch/excel-jobs", status_code=202, response_model=CreateJobResponse)
async def create_excel_job(
    request: Request,
    file: UploadFile = File(...),
    target_languages: list[str] | None = Form(default=None),
    target_languages_json: str | None = Form(default=None),
    max_language_parallelism: int | None = Form(default=None),
    teaching_mode: bool | None = Form(default=False),
    mode: str = Form(default="create"),
):
    session = await _session_config_for_request(request)
    filename = ensure_file_extension(file.filename, ".xlsx", "Only .xlsx files are allowed")
    if max_language_parallelism is not None and max_language_parallelism < 1:
        raise HTTPException(status_code=400, detail="max_language_parallelism must be >= 1")

    if mode not in {"create", "append"}:
        raise HTTPException(status_code=400, detail="mode must be 'create' or 'append'")

    parsed_languages = parse_target_languages(target_languages, target_languages_json)

    # Resolve everything the batch will need before accepting it. An Excel batch
    # runs for hours; a missing key should cost one HTTP response, not an hour.
    try:
        settings = Settings.resolve(session)
        english_voice_is_required(parsed_languages, settings.eleven)
    except (MissingSettingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    saved_path = Path(temp_file.name)
    temp_file.close()

    await save_upload_file(file, saved_path)

    job_id = uuid.uuid4().hex
    await jobs_store.create(job_id)

    spawn(
        run_excel_batch_job(
            job_id=job_id,
            excel_path=str(saved_path),
            target_languages=parsed_languages,
            max_language_parallelism=max_language_parallelism,
            jobs_store=jobs_store,
            settings=settings,
            teaching_mode=teaching_mode,
            output_dir=OUTPUT_DIR,
            mode=mode,
        ),
        name=f"excel-batch:{job_id}",
    )

    return CreateJobResponse(job_id=job_id, status="queued")


@app.get("/batch/excel-jobs/{job_id}", response_model=JobState)
async def get_excel_job(job_id: str):
    job = await jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/batch/excel-jobs/{job_id}/cancel")
async def cancel_excel_job(job_id: str):
    job = await jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = await jobs_store.request_cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, cannot cancel")
    return {"job_id": job_id, "status": "cancelling"}


@app.post("/batch/preview-excel")
async def preview_excel(file: UploadFile = File(...)):
    ensure_file_extension(file.filename, ".xlsx", "Only .xlsx files are allowed")
    import tempfile, openpyxl
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


@app.post("/stt")
async def stt_pipeline(
    request: Request,
    audio: UploadFile = File(...),
    target_language: str = Form("hi-IN"),
) -> dict:
    session = await _session_config_for_request(request)
    try:
        sarvam = SarvamSettings.resolve(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = ensure_file_extension(audio.filename, ".mp3", "Only .mp3 files are allowed")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    safe_stem_ = safe_stem(filename)
    upload_path = UPLOAD_DIR / f"{safe_stem_}-{uuid.uuid4().hex}.mp3"

    await save_upload_file(audio, upload_path)

    transcripts = transcribe_audio(
        audio_paths=[str(upload_path)],
        settings=sarvam,
        output_dir=str(OUTPUT_DIR),
        language_code="unknown",
    )

    transcript = next(iter(transcripts.values()))
    translated = translate_with_fallback(
        transcript,
        settings=sarvam,
        target_language_code=target_language,
        source_language_code="auto",
    )

    output_path = OUTPUT_DIR / f"{upload_path.stem}.{target_language}"
    tts_result = text_to_speech(
        translated,
        target_language_code=target_language,
        output_path=str(output_path),
        settings=sarvam,
        speaker="shubh",
    )

    tts_url = to_output_url(tts_result, OUTPUT_DIR)

    return {
        "input_file": filename,
        "transcript": transcript,
        "translation": translated,
        "tts_output": tts_result,
        "tts_url": tts_url,
    }


@app.post("/finalize-text")
async def finalize_text(request: FinalizeTextRequest):
    logging.info("Finalize text for language %s: %s", request.language, request.text)
    return {"status": "ok", "language": request.language, "text": request.text}


@app.post("/tts-elevenlabs")
async def tts_elevenlabs(payload: ElevenLabsTTSRequest, request: Request):
    session = await _session_config_for_request(request)
    try:
        eleven = ElevenLabsSettings.resolve(session)
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        audio_bytes = synthesize_speech_bytes(
            payload.text,
            api_key=eleven.api_key,
            config=ElevenLabsTTSConfig(
                voice_id=payload.voice_id,
                model_id=payload.model_id,
                stability=payload.stability,
                similarity_boost=payload.similarity_boost,
                style=payload.style,
                use_speaker_boost=payload.use_speaker_boost,
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ElevenLabs TTS failed: {exc}") from exc

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"elevenlabs-{uuid.uuid4().hex}.mp3"
    try:
        out_path.write_bytes(audio_bytes)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to save generated audio") from exc

    tts_url = to_output_url(out_path, OUTPUT_DIR)
    if not tts_url:
        raise HTTPException(status_code=500, detail="Failed to generate output URL")

    return {
        "tts_url": tts_url,
        "output_file": str(out_path),
    }
