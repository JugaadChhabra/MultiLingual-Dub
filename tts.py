from sarvamai import SarvamAI
from dotenv import load_dotenv
import base64
import binascii
import os
from pathlib import Path
import re


load_dotenv()


def _get_client() -> SarvamAI:
    api_key = os.getenv("SARVAM_API")
    if not api_key:
        raise ValueError("Missing SARVAM_API environment variable.")
    return SarvamAI(api_subscription_key=api_key)


def _extract_audio_bytes(response) -> bytes | None:
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)

    if isinstance(response, str):
        match = re.search(
            r"audios=\[(?:'|\")(?P<audio>.+?)(?:'|\")\]",
            response,
            re.DOTALL,
        )
        if match:
            try:
                return base64.b64decode(match.group("audio"), validate=True)
            except (ValueError, binascii.Error):
                try:
                    cleaned = re.sub(r"\s+", "", match.group("audio"))
                    return base64.b64decode(cleaned)
                except (ValueError, binascii.Error):
                    return None

    if isinstance(response, dict):
        audios = response.get("audios")
        if isinstance(audios, list) and audios:
            first = audios[0]
            if isinstance(first, str):
                try:
                    return base64.b64decode(first, validate=True)
                except (ValueError, binascii.Error):
                    try:
                        cleaned = re.sub(r"\s+", "", first)
                        return base64.b64decode(cleaned)
                    except (ValueError, binascii.Error):
                        return None
        for key in ("audio", "audio_content", "audio_data"):
            value = response.get(key)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
            if isinstance(value, str) and value.strip():
                try:
                    return base64.b64decode(value, validate=True)
                except (ValueError, binascii.Error):
                    try:
                        cleaned = re.sub(r"\s+", "", value)
                        return base64.b64decode(cleaned)
                    except (ValueError, binascii.Error):
                        return None

    audio = getattr(response, "audio", None)
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)

    audios = getattr(response, "audios", None)
    if isinstance(audios, list) and audios:
        first = audios[0]
        if isinstance(first, str):
            try:
                return base64.b64decode(first, validate=True)
            except (ValueError, binascii.Error):
                try:
                    cleaned = re.sub(r"\s+", "", first)
                    return base64.b64decode(cleaned)
                except (ValueError, binascii.Error):
                    return None

    return None


def _detect_audio_extension(audio_bytes: bytes) -> str | None:
    if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        return ".wav"
    if audio_bytes.startswith(b"ID3") or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    return None


def text_to_speech(
    text: str,
    target_language_code: str,
    output_path: str,
    speaker: str = "shubh",
    pace: float = 1.1,
    speech_sample_rate: int = 22050,
    model: str = "bulbul:v3",
) -> str:
    client = _get_client()
    response = client.text_to_speech.convert(
        text=text,
        target_language_code=target_language_code,
        speaker=speaker,
        pace=pace,
        speech_sample_rate=speech_sample_rate,
        model=model,
    )

    audio_bytes = _extract_audio_bytes(response)
    if audio_bytes:
        path = Path(output_path)
        detected_ext = _detect_audio_extension(audio_bytes)
        if not path.suffix and detected_ext:
            path = path.with_suffix(detected_ext)
        elif path.suffix and detected_ext and path.suffix.lower() != detected_ext:
            path = path.with_suffix(detected_ext)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio_bytes)
        return str(path)

    audio_url = None
    if isinstance(response, dict):
        audio_url = response.get("audio_url") or response.get("url")
    if not audio_url:
        audio_url = getattr(response, "audio_url", None) or getattr(response, "url", None)

    if isinstance(audio_url, str) and audio_url.strip():
        return audio_url

    return str(response)


if __name__ == "__main__":
    sample_text = "नमस्ते! यह एक उदाहरण है।"
    output = text_to_speech(sample_text, target_language_code="hi-IN", output_path="./output/sample.mp3")
    print(output)