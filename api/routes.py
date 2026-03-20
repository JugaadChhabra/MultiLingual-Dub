from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import uuid
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
from batch.service import run_excel_batch_job
from services.elevenlabs import ElevenLabsTTSConfig, get_elevenlabs_api_key, synthesize_speech_bytes
from batch.store import JobsStore
from services.stt import transcribe_audio
from services.tts import text_to_speech
from services.translation import translate_with_fallback
from services.wasabi import validate_s3_env
from services.runtime_config import (
    get_effective_required_status,
    get_missing_required_keys,
    parse_env_text,
    RuntimeConfig,
)


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _, s3_error = validate_s3_env()
    app.state.s3_config_error = s3_error
    yield


app = FastAPI(lifespan=lifespan)

UPLOAD_DIR = Path("./uploads")
OUTPUT_DIR = Path("./output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

jobs_store = JobsStore()
session_config_store = SessionConfigStore()


async def _runtime_config_for_request(request: Request) -> RuntimeConfig | None:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return await session_config_store.get(session_id)


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
    )


@app.get("/")
def index() -> FileResponse:
    index_path = Path("./index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path)


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint for Docker healthchecks and monitoring."""
    return {"status": "healthy", "service": "autodub"}


@app.post("/config/session-env", response_model=SessionEnvConfigResponse)
async def set_session_env_config(payload: SessionEnvConfigRequest, request: Request):
    parsed = parse_env_text(payload.env_text)
    missing_keys = get_missing_required_keys(parsed)
    if missing_keys:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Missing required keys in pasted .env text",
                "missing_keys": missing_keys,
            },
        )

    session_id = request.cookies.get(SESSION_COOKIE_NAME) or session_config_store.generate_session_id()
    await session_config_store.set(session_id, parsed)

    response = JSONResponse(
        status_code=200,
        content=SessionEnvConfigResponse(configured=True, missing_keys=[]).model_dump(),
    )
    _set_session_cookie(response, session_id)
    return response


@app.get("/config/session-env/status", response_model=SessionEnvConfigResponse)
async def get_session_env_config_status(request: Request):
    runtime_config = await _runtime_config_for_request(request)
    configured, missing_keys = get_effective_required_status(runtime_config)
    return SessionEnvConfigResponse(configured=configured, missing_keys=missing_keys)


@app.delete("/config/session-env", response_model=SessionEnvConfigResponse)
async def clear_session_env_config(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        await session_config_store.clear(session_id)
    response = JSONResponse(
        status_code=200,
        content=SessionEnvConfigResponse(configured=False, missing_keys=[]).model_dump(),
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.post("/translate")
async def translate_pipeline(payload: TranslateRequest, request: Request):
    runtime_config = await _runtime_config_for_request(request)
    transcript = payload.text
    translations = {}
    for lang in payload.target_languages:
        try:
            translated = translate_with_fallback(
                transcript,
                runtime_config=runtime_config,
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
):
    runtime_config = await _runtime_config_for_request(request)
    filename = ensure_file_extension(file.filename, ".xlsx", "Only .xlsx files are allowed")

    parsed_languages = parse_target_languages(target_languages, target_languages_json)

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    saved_path = Path(temp_file.name)
    temp_file.close()

    await save_upload_file(file, saved_path)

    job_id = uuid.uuid4().hex
    await jobs_store.create(job_id)

    asyncio.create_task(
        run_excel_batch_job(
            job_id=job_id,
            excel_path=str(saved_path),
            target_languages=parsed_languages,
            jobs_store=jobs_store,
            runtime_config=runtime_config,
        )
    )

    return CreateJobResponse(job_id=job_id, status="queued")


@app.get("/batch/excel-jobs/{job_id}", response_model=JobState)
async def get_excel_job(job_id: str):
    job = await jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/stt")
async def stt_pipeline(
    request: Request,
    audio: UploadFile = File(...),
    target_language: str = Form("hi-IN"),
) -> dict:
    runtime_config = await _runtime_config_for_request(request)
    filename = ensure_file_extension(audio.filename, ".mp3", "Only .mp3 files are allowed")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    safe_stem_ = safe_stem(filename)
    upload_path = UPLOAD_DIR / f"{safe_stem_}-{uuid.uuid4().hex}.mp3"

    await save_upload_file(audio, upload_path)

    transcripts = transcribe_audio(
        audio_paths=[str(upload_path)],
        runtime_config=runtime_config,
        output_dir=str(OUTPUT_DIR),
        language_code="unknown",
    )

    transcript = next(iter(transcripts.values()))
    translated = translate_with_fallback(
        transcript,
        runtime_config=runtime_config,
        target_language_code=target_language,
        source_language_code="auto",
    )

    output_path = OUTPUT_DIR / f"{upload_path.stem}.{target_language}"
    tts_result = text_to_speech(
        translated,
        target_language_code=target_language,
        output_path=str(output_path),
        runtime_config=runtime_config,
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
    runtime_config = await _runtime_config_for_request(request)
    try:
        audio_bytes = synthesize_speech_bytes(
            payload.text,
            api_key=get_elevenlabs_api_key(runtime_config=runtime_config),
            config=ElevenLabsTTSConfig(
                voice_id=payload.voice_id,
                model_id=payload.model_id,
                stability=payload.stability,
                similarity_boost=payload.similarity_boost,
                style=payload.style,
                use_speaker_boost=payload.use_speaker_boost,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Missing 11_LABS API key") from exc
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
