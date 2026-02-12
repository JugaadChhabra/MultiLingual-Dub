from sarvamai import SarvamAI
from dotenv import load_dotenv
import json
import os
from pathlib import Path


load_dotenv()


def _get_client() -> SarvamAI:
    api_key = os.getenv("SARVAM_API")
    if not api_key:
        raise ValueError("Missing SARVAM_API environment variable.")
    return SarvamAI(api_subscription_key=api_key)


def _extract_transcript_from_result(result: dict) -> str | None:
    for key in ("text", "transcript", "transcription"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_transcript_from_json(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if isinstance(payload, dict):
        for key in ("text", "transcript", "transcription"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value

        segments = payload.get("segments")
        if isinstance(segments, list):
            parts = [seg.get("text") for seg in segments if isinstance(seg, dict)]
            parts = [p for p in parts if isinstance(p, str) and p.strip()]
            if parts:
                return " ".join(parts)

    return None


def transcribe_audio(
    audio_paths: list[str],
    output_dir: str = "./output",
    model: str = "saaras:v3",
    mode: str = "transcribe",
    language_code: str = "unknown",
    with_diarization: bool = True,
    num_speakers: int = 2,
) -> dict[str, str]:
    client = _get_client()

    job = client.speech_to_text_job.create_job(
        model=model,
        mode=mode,
        language_code=language_code,
        with_diarization=with_diarization,
        num_speakers=num_speakers,
    )

    job.upload_files(file_paths=audio_paths)
    job.start()
    job.wait_until_complete()

    file_results = job.get_file_results()
    successful = file_results.get("successful", [])
    failed = file_results.get("failed", [])

    if failed:
        failed_details = ", ".join(
            f"{item.get('file_name', 'unknown')}: {item.get('error_message', 'unknown error')}"
            for item in failed
        )
        raise RuntimeError(f"STT failed for {len(failed)} file(s): {failed_details}")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    if successful:
        job.download_outputs(output_dir=str(output_path))

    transcripts: dict[str, str] = {}
    for item in successful:
        file_name = item.get("file_name") or "unknown"
        transcript = _extract_transcript_from_result(item)
        if not transcript and file_name != "unknown":
            stem_path = output_path / f"{Path(file_name).stem}.json"
            full_name_path = output_path / f"{Path(file_name).name}.json"
            mp3_name_path = output_path / f"{Path(file_name).name}.mp3.json"

            for json_path in (stem_path, full_name_path, mp3_name_path):
                if json_path.exists():
                    transcript = _extract_transcript_from_json(json_path)
                    if transcript:
                        break

        if transcript:
            transcripts[file_name] = transcript

    if not transcripts:
        raise RuntimeError("No transcripts were extracted from STT results.")

    return transcripts


if __name__ == "__main__":
    sample_audio = ["path/to/audio1.mp3", "path/to/audio2.mp3"]
    results = transcribe_audio(sample_audio)
    for name, text in results.items():
        print(f"\n{name}\n{text}\n")