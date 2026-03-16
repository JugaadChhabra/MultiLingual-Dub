from __future__ import annotations

import io
import logging
import os

logger = logging.getLogger(__name__)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def compress_mp3_bytes(audio_bytes: bytes) -> bytes:
    if not _bool_env("AUDIO_COMPRESS_ENABLED", True):
        return audio_bytes

    try:
        from pydub import AudioSegment
        from pydub.utils import which
    except Exception as exc:
        logger.warning("Audio compression skipped: pydub not available (%s)", exc)
        return audio_bytes

    ffmpeg_path = which("ffmpeg") or which("avconv")
    if not ffmpeg_path:
        try:
            import imageio_ffmpeg

            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
            if ffmpeg_path:
                AudioSegment.converter = ffmpeg_path
                logger.info("Using bundled ffmpeg binary at %s", ffmpeg_path)
        except Exception as exc:
            logger.warning("Bundled ffmpeg not available (%s)", exc)

    if not ffmpeg_path:
        logger.warning("Audio compression skipped: ffmpeg/libav not available")
        return audio_bytes

    bitrate = os.getenv("AUDIO_COMPRESS_BITRATE", "48k").strip() or "48k"
    sample_rate_raw = os.getenv("AUDIO_COMPRESS_SAMPLE_RATE", "24000").strip() or "24000"
    channels_raw = os.getenv("AUDIO_COMPRESS_CHANNELS", "1").strip() or "1"

    try:
        sample_rate = int(sample_rate_raw)
    except ValueError:
        sample_rate = 24000

    try:
        channels = int(channels_raw)
    except ValueError:
        channels = 1

    try:
        segment = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
        segment = segment.set_frame_rate(sample_rate).set_channels(channels)
        buffer = io.BytesIO()
        segment.export(
            buffer,
            format="mp3",
            bitrate=bitrate,
            codec="libmp3lame",
        )
        compressed = buffer.getvalue()
        if len(compressed) >= len(audio_bytes):
            logger.info(
                "Audio compression produced no size reduction (original=%d bytes, compressed=%d bytes)",
                len(audio_bytes),
                len(compressed),
            )
        else:
            logger.info(
                "Audio compression reduced size (original=%d bytes, compressed=%d bytes)",
                len(audio_bytes),
                len(compressed),
            )
        return compressed
    except Exception as exc:
        logger.warning("Audio compression failed, using original audio: %s", exc)
        return audio_bytes
