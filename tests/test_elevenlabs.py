import pytest

from services.elevenlabs import get_batch_config_for_language


def test_get_batch_config_for_english_uses_english_voice(monkeypatch) -> None:
    monkeypatch.setenv("ENGLISH_VOICE", "english-voice-id")
    config = get_batch_config_for_language("en-IN")
    assert config.voice_id == "english-voice-id"


def test_get_batch_config_for_english_requires_english_voice(monkeypatch) -> None:
    monkeypatch.delenv("ENGLISH_VOICE", raising=False)
    with pytest.raises(ValueError, match="Missing ENGLISH_VOICE"):
        get_batch_config_for_language("en-IN")


def test_get_batch_config_for_non_english_uses_desi_voice(monkeypatch) -> None:
    monkeypatch.setenv("DESI_VOCAL_VOICE", "desi-voice-id")
    config = get_batch_config_for_language("hi-IN")
    assert config.voice_id == "desi-voice-id"
