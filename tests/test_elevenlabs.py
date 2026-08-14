import pytest

from services.elevenlabs import (
    FALLBACK_DESI_VOICE_ID,
    ElevenLabsSettings,
    batch_config_for_language,
)


def _settings(**overrides) -> ElevenLabsSettings:
    base = dict(api_key="key", desi_voice_id="desi-voice-id", english_voice_id="english-voice-id")
    base.update(overrides)
    return ElevenLabsSettings(**base)


def test_english_targets_use_the_english_voice() -> None:
    config = batch_config_for_language("en-IN", _settings())

    assert config.voice_id == "english-voice-id"


def test_english_targets_require_an_english_voice() -> None:
    with pytest.raises(ValueError, match="Missing ENGLISH_VOICE"):
        batch_config_for_language("en-IN", _settings(english_voice_id=""))


def test_non_english_targets_use_the_desi_voice() -> None:
    config = batch_config_for_language("hi-IN", _settings())

    assert config.voice_id == "desi-voice-id"


def test_a_non_english_target_needs_no_english_voice() -> None:
    """An all-Hindi batch must not be blocked by an unset ENGLISH_VOICE."""
    config = batch_config_for_language("hi-IN", _settings(english_voice_id=""))

    assert config.voice_id == "desi-voice-id"


def test_desi_voice_falls_back_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("DESI_VOCAL_VOICE", raising=False)
    monkeypatch.setenv("ELEVEN_LABS", "key")

    settings = ElevenLabsSettings.resolve()

    assert settings.desi_voice_id == FALLBACK_DESI_VOICE_ID
