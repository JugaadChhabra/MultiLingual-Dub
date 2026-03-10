from __future__ import annotations
import os
from dataclasses import dataclass
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings


DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY_BOOST = 0.75
DEFAULT_STYLE = 0.0
DEFAULT_USE_SPEAKER_BOOST = True


def get_voice_id(voice_name: str = "desi") -> str:
    """Get voice ID from environment variables. voice_name can be 'desi' or 'studio'."""
    if voice_name == "studio":
        return os.getenv("AI_STUDIO_VOICE", "S1JBcZECEJJlf7lEDTbN")
    return os.getenv("DESI_VOCAL_VOICE", "dffT29nmBclERTsFHmHg")


@dataclass(frozen=True)
class ElevenLabsTTSConfig:
    voice_id: str
    model_id: str
    stability: float
    similarity_boost: float
    style: float
    use_speaker_boost: bool


def get_elevenlabs_api_key() -> str:
    api_key = os.getenv("ELEVEN_LABS", "").strip()
    if not api_key:
        raise ValueError("Missing ELEVEN_LABS API key")
    return api_key


def get_batch_default_config() -> ElevenLabsTTSConfig:
    return ElevenLabsTTSConfig(
        voice_id=get_voice_id("desi"),
        model_id=DEFAULT_MODEL_ID,
        stability=DEFAULT_STABILITY,
        similarity_boost=DEFAULT_SIMILARITY_BOOST,
        style=DEFAULT_STYLE,
        use_speaker_boost=DEFAULT_USE_SPEAKER_BOOST,
    )


def synthesize_speech_bytes(text: str, *, api_key: str, config: ElevenLabsTTSConfig) -> bytes:
    client = ElevenLabs(api_key=api_key)
    audio_stream = client.text_to_speech.convert(
        voice_id=config.voice_id,
        model_id=config.model_id,
        text=text,
        voice_settings=VoiceSettings(
            stability=config.stability,
            similarity_boost=config.similarity_boost,
            style=config.style,
            use_speaker_boost=config.use_speaker_boost,
        ),
    )

    output = bytearray()
    for chunk in audio_stream:
        output.extend(chunk)

    if not output:
        raise RuntimeError("ElevenLabs returned empty audio")
    return bytes(output)
