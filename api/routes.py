from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
import uuid
import tempfile
from threading import Lock

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.models import (
    CreateSubtitleJobResponse,
    ElevenLabsTTSRequest,
    FinalizeTextRequest,
    SessionEnvConfigRequest,
    SessionEnvConfigResponse,
    SubtitleJobState,
    SubtitleYoutubeRequest,
    TranslateRequest,
)
from api.subtitle_jobs import SubtitleJobsStore
from api.session_config import SESSION_COOKIE_NAME, SessionConfigStore
from api.utils import ensure_file_extension, parse_target_languages, safe_stem, save_upload_file, to_output_url
from batch.models import CreateJobResponse, JobState
from batch.service import run_excel_batch_job
from services.elevenlabs import ElevenLabsTTSConfig, get_elevenlabs_api_key, synthesize_speech_bytes
from batch.store import JobsStore
from services.stt import transcribe_audio
from services.subtitle_translate import translate_subtitle_texts
from services.subtitles import SubtitleCue, build_srt_for_audio_file, cue_texts, cues_with_replaced_text, write_srt_file
from services.tts import text_to_speech
from services.translation import translate_with_fallback
from services.video_ingest import convert_video_to_mp3
from services.wasabi import validate_s3_env
from services.youtube_ingest import download_youtube_media
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

_IMPORTANT_LOG_BUFFER: deque[dict[str, object]] = deque(maxlen=400)
_IMPORTANT_LOG_LOCK = Lock()
_IMPORTANT_LOG_ID = 0
_IMPORTANT_LOGGER_PREFIXES = (
    "batch",
    "services.qc",
    "services.elevenlabs",
    "api.routes",
)


def _sanitize_lang_for_filename(language_code: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in language_code).strip("_") or "lang"


def _dedupe_languages(languages: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in languages:
        value = raw.strip()
        if not value or value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _validate_subtitle_youtube_payload(payload: SubtitleYoutubeRequest) -> list[str]:
    if payload.max_chars_per_translation_chunk < 200:
        raise HTTPException(status_code=400, detail="max_chars_per_translation_chunk must be >= 200")
    return _dedupe_languages(payload.target_languages)


def _build_translated_subtitle_urls(
    *,
    cues: list[SubtitleCue],
    source_subtitle_url: str,
    output_stem: str,
    target_languages: list[str],
    runtime_config: RuntimeConfig | None,
    max_chars_per_translation_chunk: int,
) -> tuple[dict[str, str], dict[str, str]]:
    subtitle_urls: dict[str, str] = {"source": source_subtitle_url}
    translation_errors: dict[str, str] = {}

    if not target_languages:
        return subtitle_urls, translation_errors

    source_texts = cue_texts(cues)
    for language in target_languages:
        try:
            translated_texts = translate_subtitle_texts(
                source_texts,
                target_language_code=language,
                runtime_config=runtime_config,
                source_language_code="auto",
                max_chars_per_request=max_chars_per_translation_chunk,
            )
            translated_cues = cues_with_replaced_text(cues, translated_texts)
            language_file = OUTPUT_DIR / f"{output_stem}.{_sanitize_lang_for_filename(language)}.srt"
            write_srt_file(language_file, translated_cues)
            language_url = to_output_url(language_file, OUTPUT_DIR)
            if not language_url:
                raise RuntimeError("Failed to generate output URL")
            subtitle_urls[language] = language_url
        except Exception as exc:
            translation_errors[language] = str(exc)

    return subtitle_urls, translation_errors


def _run_subtitle_youtube_pipeline(
    payload: SubtitleYoutubeRequest,
    *,
    runtime_config: RuntimeConfig | None,
    parsed_languages: list[str],
) -> dict:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    download_stem = f"youtube-{uuid.uuid4().hex}"
    try:
        download_result = download_youtube_media(
            payload.youtube_url,
            output_dir=UPLOAD_DIR,
            output_stem=download_stem,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"YouTube download failed: {exc}") from exc

    source_media_path = download_result.media_path
    audio_path = UPLOAD_DIR / f"{source_media_path.stem}-{uuid.uuid4().hex}.mp3"

    try:
        convert_video_to_mp3(source_media_path, audio_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Media conversion failed: {exc}") from exc

    transcripts = transcribe_audio(
        audio_paths=[str(audio_path)],
        runtime_config=runtime_config,
        output_dir=str(OUTPUT_DIR),
        language_code="unknown",
    )

    transcript = next(iter(transcripts.values()), "")
    if not transcript.strip():
        raise HTTPException(status_code=500, detail="STT did not return transcript text")

    output_stem = f"{source_media_path.stem}-{uuid.uuid4().hex}"
    try:
        srt_path, cues = build_srt_for_audio_file(
            output_dir=OUTPUT_DIR,
            source_audio_filename=audio_path.name,
            fallback_transcript=transcript,
            output_filename=f"{output_stem}.srt",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Subtitle generation failed: {exc}") from exc

    subtitle_url = to_output_url(srt_path, OUTPUT_DIR)
    if not subtitle_url:
        raise HTTPException(status_code=500, detail="Failed to generate subtitle URL")

    subtitle_urls, translation_errors = _build_translated_subtitle_urls(
        cues=cues,
        source_subtitle_url=subtitle_url,
        output_stem=output_stem,
        target_languages=parsed_languages,
        runtime_config=runtime_config,
        max_chars_per_translation_chunk=payload.max_chars_per_translation_chunk,
    )

    return {
        "youtube_url": payload.youtube_url,
        "video_id": download_result.video_id,
        "video_title": download_result.title,
        "source_media_file": str(source_media_path),
        "audio_file": str(audio_path),
        "transcript": transcript,
        "subtitle_file": str(srt_path),
        "subtitle_url": subtitle_url,
        "subtitle_urls": subtitle_urls,
        "target_languages": parsed_languages,
        "translation_errors": translation_errors,
        "subtitle_segments": len(cues),
    }


def _run_subtitle_video_pipeline(
    *,
    input_file_name: str,
    video_path: Path,
    parsed_languages: list[str],
    max_chars_per_translation_chunk: int,
    runtime_config: RuntimeConfig | None,
) -> dict:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    audio_path = UPLOAD_DIR / f"{video_path.stem}.mp3"

    try:
        convert_video_to_mp3(video_path, audio_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Video conversion failed: {exc}") from exc

    transcripts = transcribe_audio(
        audio_paths=[str(audio_path)],
        runtime_config=runtime_config,
        output_dir=str(OUTPUT_DIR),
        language_code="unknown",
    )

    transcript = next(iter(transcripts.values()), "")
    if not transcript.strip():
        raise HTTPException(status_code=500, detail="STT did not return transcript text")

    try:
        srt_path, cues = build_srt_for_audio_file(
            output_dir=OUTPUT_DIR,
            source_audio_filename=audio_path.name,
            fallback_transcript=transcript,
            output_filename=f"{video_path.stem}.srt",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Subtitle generation failed: {exc}") from exc

    subtitle_url = to_output_url(srt_path, OUTPUT_DIR)
    if not subtitle_url:
        raise HTTPException(status_code=500, detail="Failed to generate subtitle URL")

    subtitle_urls, translation_errors = _build_translated_subtitle_urls(
        cues=cues,
        source_subtitle_url=subtitle_url,
        output_stem=video_path.stem,
        target_languages=parsed_languages,
        runtime_config=runtime_config,
        max_chars_per_translation_chunk=max_chars_per_translation_chunk,
    )

    return {
        "input_file": input_file_name,
        "audio_file": str(audio_path),
        "transcript": transcript,
        "subtitle_file": str(srt_path),
        "subtitle_url": subtitle_url,
        "subtitle_urls": subtitle_urls,
        "target_languages": parsed_languages,
        "translation_errors": translation_errors,
        "subtitle_segments": len(cues),
    }


async def run_subtitle_youtube_job(
    *,
    job_id: str,
    payload: SubtitleYoutubeRequest,
    parsed_languages: list[str],
    runtime_config: RuntimeConfig | None,
    jobs_store: SubtitleJobsStore,
) -> None:
    await jobs_store.start(job_id)
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        await jobs_store.update_progress(
            job_id,
            step="downloading",
            message="Downloading YouTube media",
            percent=10,
        )
        download_stem = f"youtube-{uuid.uuid4().hex}"
        try:
            download_result = await asyncio.to_thread(
                download_youtube_media,
                payload.youtube_url,
                output_dir=UPLOAD_DIR,
                output_stem=download_stem,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"YouTube download failed: {exc}") from exc

        source_media_path = download_result.media_path
        audio_path = UPLOAD_DIR / f"{source_media_path.stem}-{uuid.uuid4().hex}.mp3"

        await jobs_store.update_progress(
            job_id,
            step="converting",
            message="Converting media to audio",
            percent=30,
        )
        try:
            await asyncio.to_thread(convert_video_to_mp3, source_media_path, audio_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Media conversion failed: {exc}") from exc

        await jobs_store.update_progress(
            job_id,
            step="transcribing",
            message="Running speech-to-text",
            percent=50,
        )
        transcripts = await asyncio.to_thread(
            transcribe_audio,
            audio_paths=[str(audio_path)],
            runtime_config=runtime_config,
            output_dir=str(OUTPUT_DIR),
            language_code="unknown",
        )

        transcript = next(iter(transcripts.values()), "")
        if not transcript.strip():
            raise HTTPException(status_code=500, detail="STT did not return transcript text")

        output_stem = f"{source_media_path.stem}-{uuid.uuid4().hex}"
        await jobs_store.update_progress(
            job_id,
            step="rendering",
            message="Building SRT subtitles",
            percent=68,
        )
        try:
            srt_path, cues = await asyncio.to_thread(
                build_srt_for_audio_file,
                output_dir=OUTPUT_DIR,
                source_audio_filename=audio_path.name,
                fallback_transcript=transcript,
                output_filename=f"{output_stem}.srt",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Subtitle generation failed: {exc}") from exc

        subtitle_url = to_output_url(srt_path, OUTPUT_DIR)
        if not subtitle_url:
            raise HTTPException(status_code=500, detail="Failed to generate subtitle URL")

        if parsed_languages:
            await jobs_store.update_progress(
                job_id,
                step="translating",
                message="Translating subtitle cues",
                percent=85,
            )
        else:
            await jobs_store.update_progress(
                job_id,
                step="finalizing",
                message="Preparing subtitle outputs",
                percent=92,
            )

        subtitle_urls, translation_errors = await asyncio.to_thread(
            _build_translated_subtitle_urls,
            cues=cues,
            source_subtitle_url=subtitle_url,
            output_stem=output_stem,
            target_languages=parsed_languages,
            runtime_config=runtime_config,
            max_chars_per_translation_chunk=payload.max_chars_per_translation_chunk,
        )

        result = {
            "youtube_url": payload.youtube_url,
            "video_id": download_result.video_id,
            "video_title": download_result.title,
            "source_media_file": str(source_media_path),
            "audio_file": str(audio_path),
            "transcript": transcript,
            "subtitle_file": str(srt_path),
            "subtitle_url": subtitle_url,
            "subtitle_urls": subtitle_urls,
            "target_languages": parsed_languages,
            "translation_errors": translation_errors,
            "subtitle_segments": len(cues),
        }
        await jobs_store.complete(job_id, result)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            detail = exc.detail
            message = detail if isinstance(detail, str) else str(detail)
        else:
            message = str(exc)
        await jobs_store.fail(job_id, message)


async def run_subtitle_video_job(
    *,
    job_id: str,
    input_file_name: str,
    video_path: Path,
    parsed_languages: list[str],
    max_chars_per_translation_chunk: int,
    runtime_config: RuntimeConfig | None,
    jobs_store: SubtitleJobsStore,
) -> None:
    await jobs_store.start(job_id)
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        audio_path = UPLOAD_DIR / f"{video_path.stem}.mp3"

        await jobs_store.update_progress(
            job_id,
            step="converting",
            message="Converting uploaded video to audio",
            percent=22,
        )
        try:
            await asyncio.to_thread(convert_video_to_mp3, video_path, audio_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Video conversion failed: {exc}") from exc

        await jobs_store.update_progress(
            job_id,
            step="transcribing",
            message="Running speech-to-text",
            percent=48,
        )
        transcripts = await asyncio.to_thread(
            transcribe_audio,
            audio_paths=[str(audio_path)],
            runtime_config=runtime_config,
            output_dir=str(OUTPUT_DIR),
            language_code="unknown",
        )

        transcript = next(iter(transcripts.values()), "")
        if not transcript.strip():
            raise HTTPException(status_code=500, detail="STT did not return transcript text")

        await jobs_store.update_progress(
            job_id,
            step="rendering",
            message="Building SRT subtitles",
            percent=70,
        )
        try:
            srt_path, cues = await asyncio.to_thread(
                build_srt_for_audio_file,
                output_dir=OUTPUT_DIR,
                source_audio_filename=audio_path.name,
                fallback_transcript=transcript,
                output_filename=f"{video_path.stem}.srt",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Subtitle generation failed: {exc}") from exc

        subtitle_url = to_output_url(srt_path, OUTPUT_DIR)
        if not subtitle_url:
            raise HTTPException(status_code=500, detail="Failed to generate subtitle URL")

        if parsed_languages:
            await jobs_store.update_progress(
                job_id,
                step="translating",
                message="Translating subtitle cues",
                percent=86,
            )
        else:
            await jobs_store.update_progress(
                job_id,
                step="finalizing",
                message="Preparing subtitle outputs",
                percent=93,
            )

        subtitle_urls, translation_errors = await asyncio.to_thread(
            _build_translated_subtitle_urls,
            cues=cues,
            source_subtitle_url=subtitle_url,
            output_stem=video_path.stem,
            target_languages=parsed_languages,
            runtime_config=runtime_config,
            max_chars_per_translation_chunk=max_chars_per_translation_chunk,
        )

        result = {
            "input_file": input_file_name,
            "audio_file": str(audio_path),
            "transcript": transcript,
            "subtitle_file": str(srt_path),
            "subtitle_url": subtitle_url,
            "subtitle_urls": subtitle_urls,
            "target_languages": parsed_languages,
            "translation_errors": translation_errors,
            "subtitle_segments": len(cues),
        }
        await jobs_store.complete(job_id, result)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            detail = exc.detail
            message = detail if isinstance(detail, str) else str(detail)
        else:
            message = str(exc)
        await jobs_store.fail(job_id, message)


def _is_important_log_record(record: logging.LogRecord) -> bool:
    if record.levelno >= logging.WARNING:
        return True
    if record.levelno >= logging.INFO:
        name = record.name or ""
        return any(name.startswith(prefix) for prefix in _IMPORTANT_LOGGER_PREFIXES)
    return False


class ImportantLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _IMPORTANT_LOG_ID
        if not _is_important_log_record(record):
            return
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with _IMPORTANT_LOG_LOCK:
            _IMPORTANT_LOG_ID += 1
            payload["id"] = _IMPORTANT_LOG_ID
            _IMPORTANT_LOG_BUFFER.append(payload)


_root_logger = logging.getLogger()
if not any(isinstance(handler, ImportantLogHandler) for handler in _root_logger.handlers):
    important_handler = ImportantLogHandler()
    important_handler.setLevel(logging.INFO)
    _root_logger.addHandler(important_handler)


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
subtitle_jobs_store = SubtitleJobsStore()
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


@app.get("/logs/important")
async def get_important_logs(since_id: int = 0, limit: int = 200) -> dict:
    safe_limit = max(1, min(limit, 400))
    safe_since = max(0, since_id)
    with _IMPORTANT_LOG_LOCK:
        items = [item for item in _IMPORTANT_LOG_BUFFER if int(item.get("id", 0)) > safe_since]
        if len(items) > safe_limit:
            items = items[-safe_limit:]
    latest_id = items[-1]["id"] if items else safe_since
    return {"logs": items, "latest_id": latest_id}


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
    max_language_parallelism: int | None = Form(default=None),
):
    runtime_config = await _runtime_config_for_request(request)
    filename = ensure_file_extension(file.filename, ".xlsx", "Only .xlsx files are allowed")
    if max_language_parallelism is not None and max_language_parallelism < 1:
        raise HTTPException(status_code=400, detail="max_language_parallelism must be >= 1")

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
            max_language_parallelism=max_language_parallelism,
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


@app.post("/subtitle/video")
async def subtitle_from_video(
    request: Request,
    video: UploadFile = File(...),
    target_languages: list[str] | None = Form(default=None),
    target_languages_json: str | None = Form(default=None),
    max_chars_per_translation_chunk: int = Form(default=1800),
) -> dict:
    runtime_config = await _runtime_config_for_request(request)
    filename = ensure_file_extension(video.filename, ".mp4", "Only .mp4 files are allowed")
    if max_chars_per_translation_chunk < 200:
        raise HTTPException(status_code=400, detail="max_chars_per_translation_chunk must be >= 200")

    parsed_languages: list[str] = []
    if target_languages or target_languages_json:
        parsed_languages = parse_target_languages(target_languages, target_languages_json)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    safe_video_stem = safe_stem(filename)
    video_path = UPLOAD_DIR / f"{safe_video_stem}-{uuid.uuid4().hex}.mp4"

    await save_upload_file(video, video_path)
    return _run_subtitle_video_pipeline(
        input_file_name=filename,
        video_path=video_path,
        parsed_languages=parsed_languages,
        max_chars_per_translation_chunk=max_chars_per_translation_chunk,
        runtime_config=runtime_config,
    )


@app.post("/subtitle/video-jobs", status_code=202, response_model=CreateSubtitleJobResponse)
async def create_subtitle_video_job(
    request: Request,
    video: UploadFile = File(...),
    target_languages: list[str] | None = Form(default=None),
    target_languages_json: str | None = Form(default=None),
    max_chars_per_translation_chunk: int = Form(default=1800),
):
    runtime_config = await _runtime_config_for_request(request)
    filename = ensure_file_extension(video.filename, ".mp4", "Only .mp4 files are allowed")
    if max_chars_per_translation_chunk < 200:
        raise HTTPException(status_code=400, detail="max_chars_per_translation_chunk must be >= 200")

    parsed_languages: list[str] = []
    if target_languages or target_languages_json:
        parsed_languages = parse_target_languages(target_languages, target_languages_json)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_video_stem = safe_stem(filename)
    video_path = UPLOAD_DIR / f"{safe_video_stem}-{uuid.uuid4().hex}.mp4"
    await save_upload_file(video, video_path)

    job_id = uuid.uuid4().hex
    await subtitle_jobs_store.create(job_id)

    asyncio.create_task(
        run_subtitle_video_job(
            job_id=job_id,
            input_file_name=filename,
            video_path=video_path,
            parsed_languages=parsed_languages,
            max_chars_per_translation_chunk=max_chars_per_translation_chunk,
            runtime_config=runtime_config,
            jobs_store=subtitle_jobs_store,
        )
    )

    return CreateSubtitleJobResponse(job_id=job_id, status="queued")


@app.get("/subtitle/video-jobs/{job_id}", response_model=SubtitleJobState)
async def get_subtitle_video_job(job_id: str):
    job = await subtitle_jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/subtitle/youtube")
async def subtitle_from_youtube(payload: SubtitleYoutubeRequest, request: Request) -> dict:
    runtime_config = await _runtime_config_for_request(request)
    parsed_languages = _validate_subtitle_youtube_payload(payload)
    return _run_subtitle_youtube_pipeline(
        payload,
        runtime_config=runtime_config,
        parsed_languages=parsed_languages,
    )


@app.post("/subtitle/youtube-jobs", status_code=202, response_model=CreateSubtitleJobResponse)
async def create_subtitle_youtube_job(payload: SubtitleYoutubeRequest, request: Request):
    runtime_config = await _runtime_config_for_request(request)
    parsed_languages = _validate_subtitle_youtube_payload(payload)

    job_id = uuid.uuid4().hex
    await subtitle_jobs_store.create(job_id)

    asyncio.create_task(
        run_subtitle_youtube_job(
            job_id=job_id,
            payload=payload,
            parsed_languages=parsed_languages,
            runtime_config=runtime_config,
            jobs_store=subtitle_jobs_store,
        )
    )

    return CreateSubtitleJobResponse(job_id=job_id, status="queued")


@app.get("/subtitle/youtube-jobs/{job_id}", response_model=SubtitleJobState)
async def get_subtitle_youtube_job(job_id: str):
    job = await subtitle_jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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
