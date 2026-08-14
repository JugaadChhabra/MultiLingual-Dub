from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UploadedAudio:
    """An audio asset that lives on the render provider."""
    asset_id: str


@dataclass(frozen=True)
class RenderedVideo:
    """A finished render. ``video_url`` is a temporary download URL — HeyGen's
    expire in ~7 days — so callers download promptly or re-fetch via
    ``await_render`` later."""
    video_url: str
    duration_seconds: float | None = None


class VideoRenderer(ABC):
    """The seam between a video job and whoever actually renders it.

    Everything provider-specific lives behind this: HTTP, auth, retry, response
    shapes. Nothing that crosses it carries a provider's JSON — callers see
    ``UploadedAudio`` / ``RenderedVideo`` and plain strings.

    All methods are async so callers never decide how a call blocks; an adapter
    over a synchronous client wraps its own calls in ``asyncio.to_thread``.
    """

    @abstractmethod
    async def upload_audio(self, *, content: bytes, content_type: str) -> UploadedAudio:
        """Upload a rendered audio track for use as a render's voice."""

    @abstractmethod
    async def upload_photo(self, *, content: bytes, content_type: str) -> str:
        """Upload a talking photo, returning its id.

        Providers cap how many photos an account may hold; an adapter is
        expected to handle that cap itself (rotate, or raise). Deciding *when*
        to upload is the caller's job — see ``TalkingPhotoSlots``.
        """

    @abstractmethod
    async def clear_photos(self) -> int:
        """Delete every talking photo this account owns, freeing all slots.

        Best-effort: returns how many were deleted rather than failing on one
        stubborn photo. Only safe when no other render is using them.
        """

    @abstractmethod
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
        """Submit a render, returning the provider's video id.

        ``correlation_id`` tags the render so it can be found again if the
        response to this call is lost — submission is not assumed idempotent.
        """

    @abstractmethod
    async def await_render(self, *, video_id: str) -> RenderedVideo:
        """Block until the render finishes, then return it.

        Returns immediately for a render that is already complete, which is what
        makes this double as "re-fetch a fresh URL for an old render".

        Raises if the provider reports the render failed, or if it finishes
        without a usable URL.
        """

    @abstractmethod
    async def download(self, *, video_url: str, dest_path: Path) -> int:
        """Download a finished render to ``dest_path``, returning bytes written.

        Implementations are expected to survive a mid-transfer connection drop;
        a partially-downloaded file at ``dest_path`` may be resumed.
        """
