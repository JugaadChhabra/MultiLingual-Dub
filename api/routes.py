from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import uuid
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.models import ElevenLabsTTSRequest, FinalizeTextRequest, TranslateRequest
from api.utils import ensure_file_extension, parse_target_languages, safe_stem, save_upload_file, to_output_url
from batch.models import CreateJobResponse, JobState
from batch.service import run_excel_batch_job
from services.elevenlabs import ElevenLabsTTSConfig, get_elevenlabs_api_key, synthesize_speech_bytes
from batch.store import JobsStore
from services.stt import transcribe_audio
from services.tts import text_to_speech
from services.translation import translate_with_fallback
from services.wasabi import validate_s3_env


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


@app.get("/")
def index() -> FileResponse:
    index_path = Path("./index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path)


@app.post("/translate")
async def translate_pipeline(request: TranslateRequest):
    transcript = request.text
    translations = {}
    for lang in request.target_languages:
        try:
            translated = translate_with_fallback(
                transcript,
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
    file: UploadFile = File(...),
    target_languages: list[str] | None = Form(default=None),
    target_languages_json: str | None = Form(default=None),
):
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
    audio: UploadFile = File(...),
    target_language: str = Form("hi-IN"),
) -> dict:
    filename = ensure_file_extension(audio.filename, ".mp3", "Only .mp3 files are allowed")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    safe_stem_ = safe_stem(filename)
    upload_path = UPLOAD_DIR / f"{safe_stem_}-{uuid.uuid4().hex}.mp3"

    await save_upload_file(audio, upload_path)

    transcripts = transcribe_audio(
        audio_paths=[str(upload_path)],
        output_dir=str(OUTPUT_DIR),
        language_code="unknown",
    )

    transcript = next(iter(transcripts.values()))
    translated = translate_with_fallback(
        transcript,
        target_language_code=target_language,
        source_language_code="auto",
    )

    output_path = OUTPUT_DIR / f"{upload_path.stem}.{target_language}"
    tts_result = text_to_speech(
        translated,
        target_language_code=target_language,
        output_path=str(output_path),
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
async def tts_elevenlabs(request: ElevenLabsTTSRequest):
    try:
        audio_bytes = synthesize_speech_bytes(
            request.text,
            api_key=get_elevenlabs_api_key(),
            config=ElevenLabsTTSConfig(
                voice_id=request.voice_id,
                model_id=request.model_id,
                stability=request.stability,
                similarity_boost=request.similarity_boost,
                style=request.style,
                use_speaker_boost=request.use_speaker_boost,
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
