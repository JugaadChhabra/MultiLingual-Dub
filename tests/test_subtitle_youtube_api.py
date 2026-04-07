from pathlib import Path

from fastapi.testclient import TestClient

from api import routes as api
from services.subtitles import SubtitleCue
from services.youtube_ingest import YouTubeDownloadResult


def test_subtitle_youtube_rejects_low_chunk_size() -> None:
    client = TestClient(api.app)
    response = client.post(
        "/subtitle/youtube",
        json={
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "max_chars_per_translation_chunk": 100,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "max_chars_per_translation_chunk must be >= 200"


def test_subtitle_youtube_rejects_invalid_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "output")

    def fake_download_youtube_media(youtube_url: str, *, output_dir: Path, output_stem: str):
        _ = (youtube_url, output_dir, output_stem)
        raise ValueError("youtube_url must be a valid YouTube link")

    monkeypatch.setattr(api, "download_youtube_media", fake_download_youtube_media)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/youtube",
        json={"youtube_url": "https://example.com/video"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "youtube_url must be a valid YouTube link"


def test_subtitle_youtube_generates_translated_srt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "output")

    def fake_download_youtube_media(youtube_url: str, *, output_dir: Path, output_stem: str):
        _ = (youtube_url, output_stem)
        output_dir.mkdir(parents=True, exist_ok=True)
        media_path = output_dir / "downloaded-video.webm"
        media_path.write_bytes(b"fake-webm")
        return YouTubeDownloadResult(
            media_path=media_path,
            video_id="abc123",
            title="Fake Video",
        )

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
        return {"audio.mp3": "Alpha Beta"}

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
        srt_name = output_filename or "youtube-source.srt"
        srt_path = output_dir / srt_name
        srt_path.write_text("source", encoding="utf-8")
        return srt_path, [
            SubtitleCue(start_ms=0, end_ms=1000, text="Alpha"),
            SubtitleCue(start_ms=1100, end_ms=2100, text="Beta"),
        ]

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

    monkeypatch.setattr(api, "download_youtube_media", fake_download_youtube_media)
    monkeypatch.setattr(api, "convert_video_to_mp3", fake_convert)
    monkeypatch.setattr(api, "transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr(api, "build_srt_for_audio_file", fake_build_srt_for_audio_file)
    monkeypatch.setattr(api, "translate_subtitle_texts", fake_translate_subtitle_texts)

    client = TestClient(api.app)
    response = client.post(
        "/subtitle/youtube",
        json={
            "youtube_url": "https://www.youtube.com/watch?v=abc123",
            "target_languages": ["hi-IN", "hi-IN", "ta-IN"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == "abc123"
    assert payload["video_title"] == "Fake Video"
    assert payload["target_languages"] == ["hi-IN", "ta-IN"]
    assert payload["translation_errors"] == {}
    assert payload["subtitle_urls"]["source"].startswith("/output/")
    assert payload["subtitle_urls"]["hi-IN"].endswith(".hi-IN.srt")
    assert payload["subtitle_urls"]["ta-IN"].endswith(".ta-IN.srt")
