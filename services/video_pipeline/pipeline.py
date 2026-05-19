from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
from pathlib import Path

from services.elevenlabs import (
    ElevenLabsTTSConfig,
    get_elevenlabs_api_key,
    synthesize_speech_bytes,
)
from services.runtime_config import RuntimeConfig
from services.video_pipeline.heygen_client import (
    create_avatar_iv_video,
    download_video,
    get_default_voice_id,
    get_heygen_api_key,
    poll_until_done,
    upload_asset,
    upload_talking_photo,
)
from services.nas import NasService, get_nas_config
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec

logger = logging.getLogger(__name__)


def _image_dimensions(content: bytes) -> tuple[int, int] | None:
    """Return (width, height) for JPEG / PNG / WEBP bytes; None if unknown."""
    if len(content) < 24:
        return None
    # PNG: 8-byte sig + IHDR with width/height at offsets 16, 20
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", content[16:24])
        return int(w), int(h)
    # WEBP: "RIFF....WEBP"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        chunk = content[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", content[26:30])
            return int(w) & 0x3FFF, int(h) & 0x3FFF
        if chunk == b"VP8L":
            b0, b1, b2, b3 = content[21], content[22], content[23], content[24]
            w = 1 + (((b1 & 0x3F) << 8) | b0)
            h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return w, h
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(content[24:27], "little")
            h = 1 + int.from_bytes(content[27:30], "little")
            return w, h
    # JPEG: scan SOF markers
    if content[:2] == b"\xff\xd8":
        i = 2
        n = len(content)
        while i + 9 < n:
            if content[i] != 0xFF:
                return None
            # skip fill bytes
            while i < n and content[i] == 0xFF:
                i += 1
            if i >= n:
                return None
            marker = content[i]
            i += 1
            # Standalone markers (no length): RSTn (D0-D7), SOI (D8), EOI (D9), TEM (01)
            if marker in (0x01,) or 0xD0 <= marker <= 0xD9:
                continue
            if i + 1 >= n:
                return None
            seg_len = struct.unpack(">H", content[i:i+2])[0]
            # SOF markers (excluding DHT=C4, DAC=CC, JPG=C8)
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 7 > n:
                    return None
                h, w = struct.unpack(">HH", content[i+3:i+7])
                return int(w), int(h)
            i += seg_len
    return None


_HEYGEN_DIM_MAX = 4095
_HEYGEN_DIM_MIN = 128


def _clamp_heygen_dims(width: int, height: int) -> tuple[int, int]:
    """Scale (width, height) to fit HeyGen's [128, 4095] range, preserving aspect.
    Result dimensions are even (some encoders prefer even sizes)."""
    w, h = float(width), float(height)
    longest = max(w, h)
    if longest > _HEYGEN_DIM_MAX:
        scale = _HEYGEN_DIM_MAX / longest
        w *= scale
        h *= scale
    shortest = min(w, h)
    if shortest < _HEYGEN_DIM_MIN:
        scale = _HEYGEN_DIM_MIN / shortest
        w *= scale
        h *= scale
    iw = max(_HEYGEN_DIM_MIN, min(_HEYGEN_DIM_MAX, int(round(w)) // 2 * 2))
    ih = max(_HEYGEN_DIM_MIN, min(_HEYGEN_DIM_MAX, int(round(h)) // 2 * 2))
    return iw, ih


def _tts_cache_key(*, script: str, voice_id: str, model_id: str, stability: float,
                   similarity_boost: float, style: float, use_speaker_boost: bool) -> str:
    payload = json.dumps(
        {
            "script": script,
            "voice_id": voice_id,
            "model_id": model_id,
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _guess_image_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")


async def run_video_job(
    *,
    job_id: str,
    spec: VideoJobSpec,
    image_bytes: bytes,
    image_filename: str,
    output_dir: Path,
    jobs_store: VideoJobsStore,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    try:
        heygen_key = get_heygen_api_key(runtime_config=runtime_config)
        eleven_key = get_elevenlabs_api_key(runtime_config=runtime_config)

        voice_id = spec.voice_id or get_default_voice_id(runtime_config=runtime_config)
        if not voice_id:
            raise ValueError("Missing voice_id (provide one or set ISHWARI_VOICE_ID in env)")

        # 1. ElevenLabs TTS — content-hash cached so retries / repeat scripts
        # never re-bill ElevenLabs credits.
        job_dir = output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        audio_path = job_dir / "audio.mp3"

        cache_dir = output_dir / "_audio_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = _tts_cache_key(
            script=spec.script,
            voice_id=voice_id,
            model_id=spec.model_id,
            stability=spec.stability,
            similarity_boost=spec.similarity_boost,
            style=spec.style,
            use_speaker_boost=spec.use_speaker_boost,
        )
        cache_path = cache_dir / f"{cache_key}.mp3"

        audio_bytes: bytes
        if cache_path.exists() and cache_path.stat().st_size > 0:
            await jobs_store.set_status(job_id, "tts", f"Reusing cached audio ({cache_key[:12]})")
            audio_bytes = cache_path.read_bytes()
            logger.info("TTS cache hit for job %s (key=%s, %d bytes)", job_id, cache_key[:12], len(audio_bytes))
        else:
            await jobs_store.set_status(job_id, "tts", "Generating audio with ElevenLabs")
            audio_bytes = await asyncio.to_thread(
                synthesize_speech_bytes,
                spec.script,
                api_key=eleven_key,
                config=ElevenLabsTTSConfig(
                    voice_id=voice_id,
                    model_id=spec.model_id,
                    stability=spec.stability,
                    similarity_boost=spec.similarity_boost,
                    style=spec.style,
                    use_speaker_boost=spec.use_speaker_boost,
                ),
            )
            cache_path.write_bytes(audio_bytes)
            logger.info("TTS cache write for job %s (key=%s, %d bytes)", job_id, cache_key[:12], len(audio_bytes))

        audio_path.write_bytes(audio_bytes)
        await jobs_store.patch_summary(
            job_id,
            audio_bytes=len(audio_bytes),
            audio_path=str(audio_path),
        )

        # 2. Upload audio (asset) + resolve talking photo
        await jobs_store.set_status(job_id, "uploading", "Uploading audio to HeyGen")
        audio_asset = await asyncio.to_thread(
            upload_asset, api_key=heygen_key, content=audio_bytes, content_type="audio/mpeg"
        )

        if spec.talking_photo_id:
            talking_photo_id = spec.talking_photo_id
        else:
            await jobs_store.set_status(job_id, "uploading", "Uploading talking photo to HeyGen")
            image_content_type = _guess_image_content_type(image_filename)
            talking_photo_id = await asyncio.to_thread(
                upload_talking_photo,
                api_key=heygen_key,
                content=image_bytes,
                content_type=image_content_type,
            )
        await jobs_store.patch_summary(
            job_id,
            audio_asset_id=audio_asset.asset_id,
            image_key=talking_photo_id,
        )

        # 3. Create Avatar IV video via /v2/video/generate (Talking Photo + use_avatar_iv_model)
        # Match the source image's aspect ratio so HeyGen doesn't pad with a white border
        # or fall back to its default 1920x1080 landscape.
        out_w, out_h = spec.width, spec.height
        if (not out_w or not out_h) and image_bytes:
            dims = _image_dimensions(image_bytes)
            if dims:
                out_w, out_h = _clamp_heygen_dims(*dims)
                logger.info(
                    "Detected image dims %sx%s, clamped to %sx%s for job %s",
                    dims[0], dims[1], out_w, out_h, job_id,
                )
            else:
                logger.warning("Could not detect image dimensions for job %s; HeyGen will use defaults", job_id)

        await jobs_store.set_status(job_id, "generating", "Submitting Avatar IV render")
        video_id = await asyncio.to_thread(
            create_avatar_iv_video,
            api_key=heygen_key,
            talking_photo_id=talking_photo_id,
            audio_asset_id=audio_asset.asset_id,
            motion_prompt=spec.motion_prompt or spec.video_prompt,
            width=out_w,
            height=out_h,
            video_title=spec.video_title,
            callback_id=job_id,
        )
        await jobs_store.patch_summary(job_id, heygen_video_id=video_id)

        # 4. Poll until complete
        await jobs_store.set_status(job_id, "polling", f"Polling render status (video_id={video_id})")
        result = await poll_until_done(api_key=heygen_key, video_id=video_id)
        video_url = result.get("video_url")
        if not video_url:
            raise RuntimeError("HeyGen completed but returned no video_url")

        # 5. Download to local storage (URL expires in 7 days)
        await jobs_store.set_status(job_id, "downloading", "Downloading rendered video")
        video_path = job_dir / "video.mp4"
        await asyncio.to_thread(download_video, video_url, str(video_path))
        await jobs_store.patch_summary(
            job_id,
            video_url=video_url,
            video_path=str(video_path),
        )

        # 6. Upload to NAS
        await jobs_store.set_status(job_id, "nas_upload", "Uploading to NAS")
        nas_config = get_nas_config(runtime_config=runtime_config)
        nas = NasService(nas_config)
        nas_path = await asyncio.to_thread(
            nas.upload_video, job_id, spec.video_title, str(video_path)
        )
        await jobs_store.patch_summary(job_id, nas_path=nas_path)

        await jobs_store.complete(job_id)
    except Exception as exc:
        logger.exception("Video job %s failed", job_id)
        await jobs_store.fail(job_id, str(exc))
