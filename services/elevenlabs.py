from __future__ import annotations
from dataclasses import dataclass
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
import httpx

from services.retry import retry_call
from services.runtime_config import RuntimeConfig, read_setting, require


DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY_BOOST = 0.75
DEFAULT_STYLE = 0.0
DEFAULT_USE_SPEAKER_BOOST = True

# Used when DESI_VOCAL_VOICE is unset, which is why that key is not required.
FALLBACK_DESI_VOICE_ID = "dffT29nmBclERTsFHmHg"


def is_english_language(language_code: str) -> bool:
    return language_code.strip().lower().startswith("en")


@dataclass(frozen=True)
class ElevenLabsSettings:
    api_key: str
    desi_voice_id: str
    # Empty unless configured. Only needed when a job actually targets English,
    # so it is checked against the request rather than required up front.
    english_voice_id: str

    REQUIRED = ("ELEVEN_LABS",)

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> ElevenLabsSettings:
        values = require(cls.REQUIRED, session)
        return cls(
            api_key=values["ELEVEN_LABS"],
            desi_voice_id=read_setting("DESI_VOCAL_VOICE", session) or FALLBACK_DESI_VOICE_ID,
            english_voice_id=read_setting("ENGLISH_VOICE", session),
        )

    def voice_for_language(self, language_code: str) -> str:
        if is_english_language(language_code):
            if not self.english_voice_id:
                raise ValueError("Missing ENGLISH_VOICE for English audio generation")
            return self.english_voice_id
        return self.desi_voice_id


@dataclass(frozen=True)
class ElevenLabsTTSConfig:
    voice_id: str
    model_id: str
    stability: float
    similarity_boost: float
    style: float
    use_speaker_boost: bool


def batch_config_for_language(
    language_code: str, settings: ElevenLabsSettings
) -> ElevenLabsTTSConfig:
    return ElevenLabsTTSConfig(
        voice_id=settings.voice_for_language(language_code),
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
