from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import uuid

from stt import transcribe_audio
from translate import translate_text
from tts import text_to_speech


load_dotenv()

app = FastAPI()

UPLOAD_DIR = Path("./uploads")
OUTPUT_DIR = Path("./output")

app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


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
