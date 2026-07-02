from pathlib import Path

import pytest

from services.video_pipeline.pipeline import _tts_cache_key, resolve_audio_path


def test_cache_key_differs_by_speed() -> None:
    common = dict(
        script="hi", voice_id="v", model_id="eleven_multilingual_v2",
        stability=0.5, similarity_boost=0.75, style=0.0, use_speaker_boost=True,
    )
    assert _tts_cache_key(**common, speed=1.0) != _tts_cache_key(**common, speed=0.9)


def test_resolve_audio_path_ok(tmp_path: Path) -> None:
    f = tmp_path / "elevenlabs-abc.mp3"
    f.write_bytes(b"x")
    assert resolve_audio_path("elevenlabs-abc", tmp_path) == f.resolve()


def test_resolve_audio_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_audio_path("../../etc/passwd", tmp_path)


def test_resolve_audio_path_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_audio_path("nope", tmp_path)
