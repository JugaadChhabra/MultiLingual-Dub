from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg


def convert_video_to_mp3(
    video_path: str | Path,
    output_mp3_path: str | Path,
    *,
    sample_rate_hz: int = 16000,
    channels: int = 1,
    bitrate: str = "128k",
) -> Path:
    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(f"Video file not found: {src}")

    dst = Path(output_mp3_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate_hz),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        str(dst),
    ]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"ffmpeg conversion failed: {stderr or 'unknown error'}")

    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg conversion completed but output MP3 is missing or empty")

    return dst
