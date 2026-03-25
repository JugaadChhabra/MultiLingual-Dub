from __future__ import annotations
from dataclasses import dataclass
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
import httpx

from services.retry import retry_call
from services.runtime_config import RuntimeConfig, get_config_value


DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY_BOOST = 0.75
DEFAULT_STYLE = 0.0
DEFAULT_USE_SPEAKER_BOOST = True


def is_english_language(language_code: str) -> bool:
    return language_code.strip().lower().startswith("en")


def get_voice_id(voice_name: str = "desi", runtime_config: RuntimeConfig | None = None) -> str:
    """
    Get voice ID from environment variables.

    Supported voice_name values:
    - "desi": DESI_VOCAL_VOICE (default)
    - "studio": AI_STUDIO_VOICE
    - "english": ENGLISH_VOICE
    """
    if voice_name == "studio":
        return get_config_value("AI_STUDIO_VOICE", runtime_config=runtime_config) or "S1JBcZECEJJlf7lEDTbN"
    if voice_name == "english":
        voice_id = get_config_value("ENGLISH_VOICE", runtime_config=runtime_config)
        if not voice_id:
            raise ValueError("Missing ENGLISH_VOICE for English audio generation")
        return voice_id
    return get_config_value("DESI_VOCAL_VOICE", runtime_config=runtime_config) or "dffT29nmBclERTsFHmHg"


@dataclass(frozen=True)
class ElevenLabsTTSConfig:
    voice_id: str
    model_id: str
    stability: float
    similarity_boost: float
    style: float
    use_speaker_boost: bool


def get_elevenlabs_api_key(runtime_config: RuntimeConfig | None = None) -> str:
    api_key = get_config_value("ELEVEN_LABS", runtime_config=runtime_config)
    if not api_key:
        raise ValueError("Missing ELEVEN_LABS API key")
    return api_key


def get_batch_default_config(runtime_config: RuntimeConfig | None = None) -> ElevenLabsTTSConfig:
    return ElevenLabsTTSConfig(
        voice_id=get_voice_id("desi", runtime_config=runtime_config),
        model_id=DEFAULT_MODEL_ID,
        stability=DEFAULT_STABILITY,
        similarity_boost=DEFAULT_SIMILARITY_BOOST,
        style=DEFAULT_STYLE,
        use_speaker_boost=DEFAULT_USE_SPEAKER_BOOST,
    )


def get_batch_config_for_language(
    language_code: str,
    runtime_config: RuntimeConfig | None = None,
) -> ElevenLabsTTSConfig:
    voice_name = "english" if is_english_language(language_code) else "desi"
    return ElevenLabsTTSConfig(
        voice_id=get_voice_id(voice_name, runtime_config=runtime_config),
        model_id=DEFAULT_MODEL_ID,
        stability=DEFAULT_STABILITY,
        similarity_boost=DEFAULT_SIMILARITY_BOOST,
        style=DEFAULT_STYLE,
        use_speaker_boost=DEFAULT_USE_SPEAKER_BOOST,
    )


def _synthesize_once(text: str, *, api_key: str, config: ElevenLabsTTSConfig) -> bytes:
    # Set httpx timeout: 20 second total timeout to prevent indefinite hangs
    # connect=5.0: time to establish TCP connection
    # pool=5.0: time to acquire connection from pool
    timeout = httpx.Timeout(20.0, connect=5.0, pool=5.0)
    client = ElevenLabs(api_key=api_key, httpx_client=httpx.Client(timeout=timeout))
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


def synthesize_speech_bytes(text: str, *, api_key: str, config: ElevenLabsTTSConfig) -> bytes:
    return retry_call(
        lambda: _synthesize_once(text, api_key=api_key, config=config),
        operation="ElevenLabs TTS",
    )
