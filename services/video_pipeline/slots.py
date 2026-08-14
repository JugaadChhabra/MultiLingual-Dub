from __future__ import annotations

import logging
from pathlib import Path

from services.video_pipeline.renderer import VideoRenderer

logger = logging.getLogger(__name__)


def guess_image_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")


class TalkingPhotoSlots:
    """Owns the rule for getting a usable talking photo out of a capped account.

    HeyGen allows an account only three photo avatars. Uploading past the cap
    triggers a list -> delete -> re-upload rotation on *every* render, which is
    pure serial overhead, and leftover photos from an earlier run can make an
    upload fail outright. So the rule is: free every slot, then upload once.

    Who calls ``acquire``, and how often, is the caller's decision and the thing
    that differs between the two pipelines:

    - a single generation acquires one photo for its one render
    - a batch acquires ONE photo up front and reuses that id for every row,
      because every row shares the same image

    Not a seam — a plain module over ``VideoRenderer``. That keeps this logic
    running for real in tests instead of being stubbed out behind a fake.
    """

    def __init__(self, renderer: VideoRenderer) -> None:
        self._renderer = renderer

    async def acquire(self, *, image_bytes: bytes, image_filename: str, label: str = "") -> str:
        """Free all slots, then upload ``image_bytes``. Returns the photo id.

        The clear is best-effort: if it fails, the upload is still attempted,
        since the account may well have had a free slot anyway.
        """
        prefix = f"{label} | " if label else ""
        try:
            deleted = await self._renderer.clear_photos()
            if deleted:
                logger.info("%sfreed %d talking photo slot(s) before upload", prefix, deleted)
        except Exception as exc:
            logger.warning("%spre-upload slot clear failed (continuing): %s", prefix, exc)

        photo_id = await self._renderer.upload_photo(
            content=image_bytes,
            content_type=guess_image_content_type(image_filename),
        )
        logger.info("%sacquired talking photo %s", prefix, photo_id)
        return photo_id
