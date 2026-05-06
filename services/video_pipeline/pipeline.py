from __future__ import annotations

import asyncio
import logging
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
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec

logger = logging.getLogger(__name__)


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

        # 1. ElevenLabs TTS
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

        job_dir = output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        audio_path = job_dir / "audio.mp3"
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
        await jobs_store.set_status(job_id, "generating", "Submitting Avatar IV render")
        video_id = await asyncio.to_thread(
            create_avatar_iv_video,
            api_key=heygen_key,
            talking_photo_id=talking_photo_id,
            audio_asset_id=audio_asset.asset_id,
            motion_prompt=spec.motion_prompt or spec.video_prompt,
            width=spec.width,
            height=spec.height,
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

        await jobs_store.complete(job_id)
    except Exception as exc:
        logger.exception("Video job %s failed", job_id)
        await jobs_store.fail(job_id, str(exc))
