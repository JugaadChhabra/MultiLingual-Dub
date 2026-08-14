"""In-memory adapters for the video pipeline's seams.

These are the second adapter for each interface — the one that makes the seam
worth having. They record what was asked of them so tests can assert on the
sequence of provider calls (was the slot cleared before the upload? did a batch
upload exactly one photo?) without a network.
"""
from __future__ import annotations

from pathlib import Path

from services.elevenlabs import ElevenLabsTTSConfig
from services.video_pipeline.renderer import RenderedVideo, UploadedAudio, VideoRenderer
from services.video_pipeline.speech import SpeechSynth


class FakeRenderer(VideoRenderer):
    def __init__(
        self,
        *,
        video_bytes: bytes = b"fake-mp4-content",
        fail_download: bool = False,
        render_failed: bool = False,
    ) -> None:
        self.video_bytes = video_bytes
        self.fail_download = fail_download
        self.render_failed = render_failed

        # An ordered log of every provider operation, for assertions about
        # sequence — e.g. that clear_photos precedes upload_photo.
        self.calls: list[str] = []
        self.uploaded_audio: list[bytes] = []
        self.uploaded_photos: list[tuple[bytes, str]] = []
        self.submissions: list[dict] = []
        self.downloads: list[str] = []
        self.clear_count = 0
        self._next_video_id = 0

    async def upload_audio(self, *, content: bytes, content_type: str) -> UploadedAudio:
        self.calls.append("upload_audio")
        self.uploaded_audio.append(content)
        return UploadedAudio(asset_id=f"asset-{len(self.uploaded_audio)}")

    async def upload_photo(self, *, content: bytes, content_type: str) -> str:
        self.calls.append("upload_photo")
        self.uploaded_photos.append((content, content_type))
        return f"photo-{len(self.uploaded_photos)}"

    async def clear_photos(self) -> int:
        self.calls.append("clear_photos")
        self.clear_count += 1
        return 0

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
        self.calls.append("submit")
        self._next_video_id += 1
        video_id = f"video-{self._next_video_id}"
        self.submissions.append({
            "video_id": video_id,
            "photo_id": photo_id,
            "audio_asset_id": audio_asset_id,
            "motion_prompt": motion_prompt,
            "width": width,
            "height": height,
            "video_title": video_title,
            "correlation_id": correlation_id,
        })
        return video_id

    async def await_render(self, *, video_id: str) -> RenderedVideo:
        self.calls.append("await_render")
        if self.render_failed:
            raise RuntimeError("render failed: fake")
        return RenderedVideo(video_url=f"https://fake.invalid/{video_id}.mp4")

    async def download(self, *, video_url: str, dest_path: Path) -> int:
        self.calls.append("download")
        self.downloads.append(video_url)
        if self.fail_download:
            raise RuntimeError("download failed: fake")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(self.video_bytes)
        return len(self.video_bytes)


class FakeSpeech(SpeechSynth):
    def __init__(self, *, audio: bytes = b"fake-mp3-audio", fail: bool = False) -> None:
        self.audio = audio
        self.fail = fail
        self.calls: list[tuple[str, ElevenLabsTTSConfig]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def synthesize(self, text: str, *, config: ElevenLabsTTSConfig) -> bytes:
        self.calls.append((text, config))
        if self.fail:
            raise RuntimeError("tts failed: fake")
        return self.audio
