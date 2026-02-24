from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
from fastapi import Query
import logging
from fastapi import Body
from pydantic import BaseModel
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import uuid, os

from stt import transcribe_audio
from translate import translate_text
from tts import text_to_speech


load_dotenv()

app = FastAPI()

UPLOAD_DIR = Path("./uploads")
OUTPUT_DIR = Path("./output")

app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


class TranslateRequest(BaseModel):
    text: str
    target_languages: list[str]


@app.post("/translate")
async def translate_pipeline(request: TranslateRequest):
    transcript = request.text
    target_languages = request.target_languages
    translations = {}
    for lang in target_languages:
        try:
            translated = translate_text(
                transcript,
                target_language_code=lang,
                source_language_code="auto",
            )
        except Exception as exc:
            if "Source and target languages must be different" in str(exc):
                translated = transcript
            else:
                translated = f"[Translation error: {exc}]"
        translations[lang] = translated
    return {
        "input_text": transcript,
        "translations": translations,
    }

@app.get("/")
def index() -> FileResponse:
    index_path = Path("./index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path)


@app.post("/stt")
async def stt_pipeline(
    audio: UploadFile = File(...),
    target_language: str = Form("hi-IN"),
) -> dict:
    if not audio.filename:
        raise HTTPException(status_code=400, detail="Missing audio filename")

    suffix = Path(audio.filename).suffix.lower()
    if suffix != ".mp3":
        raise HTTPException(status_code=400, detail="Only .mp3 files are allowed")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    safe_stem = Path(audio.filename).stem.replace(" ", "_")
    upload_path = UPLOAD_DIR / f"{safe_stem}-{uuid.uuid4().hex}.mp3"

    try:
        contents = await audio.read()
        upload_path.write_bytes(contents)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to save upload") from exc

    transcripts = transcribe_audio(
        audio_paths=[str(upload_path)],
        output_dir=str(OUTPUT_DIR),
        language_code="unknown",
    )

    transcript = next(iter(transcripts.values()))
    try:
        translated = translate_text(
            transcript,
            target_language_code=target_language,
            source_language_code="auto",
        )
    except Exception as exc:
        if "Source and target languages must be different" in str(exc):
            translated = transcript
        else:
            raise

    output_path = OUTPUT_DIR / f"{upload_path.stem}.{target_language}"
    tts_result = text_to_speech(
        translated,
        target_language_code=target_language,
        output_path=str(output_path),
        speaker="shubh",
    )

    tts_url = None
    tts_path = Path(tts_result)
    try:
        rel_path = tts_path.resolve().relative_to(OUTPUT_DIR.resolve())
        if tts_path.exists():
            tts_url = f"/output/{rel_path.as_posix()}"
    except ValueError:
        pass

    return {
        "input_file": audio.filename,
        "transcript": transcript,
        "translation": translated,
        "tts_output": tts_result,
        "tts_url": tts_url,
    }

class FinalizeTextRequest(BaseModel):
    text: str
    language: str

@app.post("/finalize-text")
async def finalize_text(request: FinalizeTextRequest):
    logging.info(f"Finalize text for language {request.language}: {request.text}")
    print(f"[LOG] Finalized text for {request.language}: {request.text}")
    return {"status": "ok", "language": request.language, "text": request.text}

class ElevenLabsTTSRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_v3"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True

@app.post("/tts-elevenlabs")
async def tts_elevenlabs(request: ElevenLabsTTSRequest):
    api_key = os.getenv("ELEVEN_LABS")
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing 11_LABS API key")
    client = ElevenLabs(api_key=api_key)
    audio_data = client.text_to_speech.convert(
        voice_id=request.voice_id,
        model_id=request.model_id,
        text=request.text,
        voice_settings=VoiceSettings(
            stability=request.stability,
            similarity_boost=request.similarity_boost,
            style=request.style,
            use_speaker_boost=request.use_speaker_boost
        )
    )
    # Save to file (unique name)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    out_path = OUTPUT_DIR / f"elevenlabs-{uuid.uuid4().hex}.mp3"
    with open(out_path, "wb") as f:
        for chunk in audio_data:
            f.write(chunk)
    rel_path = out_path.resolve().relative_to(OUTPUT_DIR.resolve())
    tts_url = f"/output/{rel_path.as_posix()}"
    return {"tts_url": tts_url, "output_file": str(out_path)}