from __future__ import annotations

import asyncio
from pathlib import Path

from services.video_pipeline import heygen_client
from services.video_pipeline.renderer import RenderedVideo, UploadedAudio, VideoRenderer


class HeyGenRenderer(VideoRenderer):
    """VideoRenderer backed by HeyGen's Avatar IV API.

    Deliberately thin. Every hard-won behaviour — non-idempotent submit and
    callback_id adoption, Range-resumed downloads, photo-cap rotation, retry on
    the whole TransportError family — stays in ``heygen_client``, which earned
    those rules one production incident at a time. This class exists to make
    those calls substitutable, and to stop HeyGen's response shapes leaking into
    the job that orchestrates them.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def upload_audio(self, *, content: bytes, content_type: str) -> UploadedAudio:
        result = await asyncio.to_thread(
            heygen_client.upload_asset,
            api_key=self._api_key,
            content=content,
            content_type=content_type,
        )
        return UploadedAudio(asset_id=result.asset_id)

    async def upload_photo(self, *, content: bytes, content_type: str) -> str:
        return await asyncio.to_thread(
            heygen_client.upload_talking_photo,
            api_key=self._api_key,
            content=content,
            content_type=content_type,
        )

    async def clear_photos(self) -> int:
        return await asyncio.to_thread(
            heygen_client.clear_talking_photos, api_key=self._api_key
        )

    async def submit(
        self,
        *,
        photo_id: str,
        audio_asset_id: str,
        motion_prompt: str | None,
        width: int | None,
        height: int | None,
        video_title: str,
        correlation_id: str | None,
    ) -> str:
        return await asyncio.to_thread(
            heygen_client.create_avatar_iv_video,
            api_key=self._api_key,
            talking_photo_id=photo_id,
            audio_asset_id=audio_asset_id,
            motion_prompt=motion_prompt,
            width=width,
            height=height,
            video_title=video_title,
            callback_id=correlation_id,
        )

    async def await_render(self, *, video_id: str) -> RenderedVideo:
        # poll_until_done is already async (it sleeps between polls rather than
        # pinning a thread for the ~25 minutes a render can take).
        data = await heygen_client.poll_until_done(api_key=self._api_key, video_id=video_id)
        video_url = data.get("video_url")
        if not video_url:
            raise RuntimeError("HeyGen reported the render complete but returned no video_url")
        duration = data.get("duration")
        return RenderedVideo(
            video_url=str(video_url),
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        )

    async def download(self, *, video_url: str, dest_path: Path) -> int:
        return await asyncio.to_thread(
            heygen_client.download_video, video_url, str(dest_path)
        )
