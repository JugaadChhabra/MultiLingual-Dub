from unittest.mock import MagicMock, patch

from services.elevenlabs import (
    AUDIO_ONLY_MODEL_ID,
    ElevenLabsTTSConfig,
    _synthesize_once,
)


def test_audio_only_model_constant() -> None:
    assert AUDIO_ONLY_MODEL_ID == "eleven_multilingual_v2"


def test_config_defaults_speed_to_one() -> None:
    cfg = ElevenLabsTTSConfig(
        voice_id="v", model_id="eleven_v3", stability=0.5,
        similarity_boost=0.75, style=0.0, use_speaker_boost=True,
    )
    assert cfg.speed == 1.0


def test_synthesize_passes_speed_to_voice_settings() -> None:
    cfg = ElevenLabsTTSConfig(
        voice_id="v", model_id=AUDIO_ONLY_MODEL_ID, stability=0.3,
        similarity_boost=0.6, style=0.1, use_speaker_boost=False, speed=0.9,
    )
    fake_client = MagicMock()
    fake_client.text_to_speech.convert.return_value = [b"audio-bytes"]
    with patch("services.elevenlabs.ElevenLabs", return_value=fake_client):
        out = _synthesize_once("hello", api_key="k", config=cfg)
    assert out == b"audio-bytes"
    kwargs = fake_client.text_to_speech.convert.call_args.kwargs
    assert kwargs["model_id"] == "eleven_multilingual_v2"
    assert kwargs["voice_settings"].speed == 0.9
