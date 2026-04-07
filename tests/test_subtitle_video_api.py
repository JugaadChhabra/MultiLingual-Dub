from pathlib import Path

from fastapi.testclient import TestClient

from api import routes as api
from services.subtitles import SubtitleCue


def test_subtitle_video_rejects_non_mp4() -> None:
    client = TestClient(api.app)
    response = client.post(
        "/subtitle/video",
        files={"video": ("bad.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only .mp4 files are allowed"


def test_subtitle_video_generates_srt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "output")

    def fake_convert(video_path: str | Path, output_mp3_path: str | Path, **_kwargs) -> Path:
        mp3_path = Path(output_mp3_path)
        mp3_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_path.write_bytes(b"fake-mp3")
        return mp3_path

    def fake_transcribe_audio(
        audio_paths,
        runtime_config=None,
        output_dir="./output",
        model="saaras:v3",
        mode="transcribe",
        language_code="unknown",
        with_diarization=True,
        num_speakers=2,
    ):
        _ = (runtime_config, output_dir, model, mode, language_code, with_diarization, num_speakers)
        return {Path(audio_paths[0]).name: "Hello world. This is a test subtitle."}

    def fake_build_srt_for_audio_file(
        *,
        output_dir: Path,
        source_audio_filename: str,
        fallback_transcript: str,
        output_filename: str | None = None,
        max_chars_per_line: int = 42,
        max_lines: int = 2,
    ):
        _ = (source_audio_filename, fallback_transcript, max_chars_per_line, max_lines)
        output_dir.mkdir(parents=True, exist_ok=True)
        srt_name = output_filename or "test.srt"
        srt_path = output_dir / srt_name
        srt_path.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nHello world\n",
            encoding="utf-8",
        )
        return srt_path, [object(), object()]

    monkeypatch.setattr(api, "convert_video_to_mp3", fake_convert)
    monkeypatch.setattr(api, "transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr(api, "build_srt_for_audio_file", fake_build_srt_for_audio_file)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/video",
        files={"video": ("clip.mp4", b"fake-video", "video/mp4")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["input_file"] == "clip.mp4"
    assert payload["subtitle_url"].startswith("/output/")
    assert payload["subtitle_urls"]["source"].startswith("/output/")
    assert payload["translation_errors"] == {}
    assert payload["subtitle_segments"] == 2


def test_subtitle_video_generates_translated_srt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "output")

    def fake_convert(video_path: str | Path, output_mp3_path: str | Path, **_kwargs) -> Path:
        _ = video_path
        mp3_path = Path(output_mp3_path)
        mp3_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_path.write_bytes(b"fake-mp3")
        return mp3_path

    def fake_transcribe_audio(
        audio_paths,
        runtime_config=None,
        output_dir="./output",
        model="saaras:v3",
        mode="transcribe",
        language_code="unknown",
        with_diarization=True,
        num_speakers=2,
    ):
        _ = (audio_paths, runtime_config, output_dir, model, mode, language_code, with_diarization, num_speakers)
        return {"clip.mp3": "One. Two."}

    def fake_build_srt_for_audio_file(
        *,
        output_dir: Path,
        source_audio_filename: str,
        fallback_transcript: str,
        output_filename: str | None = None,
        max_chars_per_line: int = 42,
        max_lines: int = 2,
    ):
        _ = (
            output_dir,
            source_audio_filename,
            fallback_transcript,
            output_filename,
            max_chars_per_line,
            max_lines,
        )
        cue_a = SubtitleCue(start_ms=0, end_ms=1200, text="One")
        cue_b = SubtitleCue(start_ms=1250, end_ms=2400, text="Two")
        source = Path(api.OUTPUT_DIR) / "clip.source.srt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("dummy", encoding="utf-8")
        return source, [cue_a, cue_b]

    def fake_translate_subtitle_texts(
        texts: list[str],
        *,
        target_language_code: str,
        runtime_config=None,
        source_language_code="auto",
        max_chars_per_request=1800,
    ) -> list[str]:
        _ = (runtime_config, source_language_code, max_chars_per_request)
        return [f"{target_language_code}:{text}" for text in texts]

    monkeypatch.setattr(api, "convert_video_to_mp3", fake_convert)
    monkeypatch.setattr(api, "transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr(api, "build_srt_for_audio_file", fake_build_srt_for_audio_file)
    monkeypatch.setattr(api, "translate_subtitle_texts", fake_translate_subtitle_texts)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/video",
        files={"video": ("clip.mp4", b"fake-video", "video/mp4")},
        data={"target_languages_json": '["hi-IN","ta-IN"]'},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["target_languages"] == ["hi-IN", "ta-IN"]
    assert payload["translation_errors"] == {}
    assert payload["subtitle_urls"]["source"].startswith("/output/")
    assert payload["subtitle_urls"]["hi-IN"].endswith(".hi-IN.srt")
    assert payload["subtitle_urls"]["ta-IN"].endswith(".ta-IN.srt")


def test_subtitle_video_rejects_low_chunk_size() -> None:
    client = TestClient(api.app)
    response = client.post(
        "/subtitle/video",
        files={"video": ("clip.mp4", b"fake-video", "video/mp4")},
        data={"max_chars_per_translation_chunk": "50"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "max_chars_per_translation_chunk must be >= 200"
