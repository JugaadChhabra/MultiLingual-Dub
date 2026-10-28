"""What the routers share: the job stores, the output folders, and the builders
that turn the .env into the seams a video job runs against.

Routers read these as ``shared.<name>`` at call time rather than importing the
names, so a test can swap one out on this module and every router sees it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from batch.store import JobsStore
from services.elevenlabs import ElevenLabsSettings
from services.email import EmailSettings
from services.nas import NasConfig
from services.runtime_config import MissingSettingError
from services.script_history import ScriptHistoryStore
from services.script_writer import ScriptWriter
from services.video_pipeline import VideoJobsStore
from services.video_pipeline.batch_store import VideoBatchJobsStore
from services.video_pipeline.control import JobControls
from services.video_pipeline.heygen_client import HeyGenSettings
from services.video_pipeline.heygen_renderer import HeyGenRenderer
from services.video_pipeline.library_service import LibraryService
from services.video_pipeline.renderer import VideoRenderer
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.speech import ElevenLabsSpeech, SpeechSynth

OUTPUT_DIR = Path("./output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_OUTPUT_DIR = OUTPUT_DIR / "heygen"
VIDEO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# All three stores mirror to disk so a restart leaves a record rather than a
# 404. Only video jobs can actually be resumed from theirs — a HeyGen render
# outlives the process — but knowing how far a batch got is worth the file.
jobs_store = JobsStore(persist_dir=OUTPUT_DIR / "batch" / "_jobs")
video_jobs_store = VideoJobsStore(persist_dir=VIDEO_OUTPUT_DIR / "_jobs")
video_batch_jobs_store = VideoBatchJobsStore(persist_dir=VIDEO_OUTPUT_DIR / "_batches")
# Pause / cancel for the video jobs and batches running now, by their id.
video_controls = JobControls()

# Every HeyGen render, matched to its job; holds the listing and media caches.
video_library = LibraryService(
    video_jobs_store, jobs_dir=VIDEO_OUTPUT_DIR, cache_dir=VIDEO_OUTPUT_DIR / "_library",
)

# Generated scripts, kept so the writer can be shown what it already said. Not a
# job store: nothing here is in flight, and every read comes off disk.
script_history = ScriptHistoryStore(VIDEO_OUTPUT_DIR / "_scripts")
# Reads and records that history itself, both when a day is written and when
# its rows are submitted; the routes only call it.
script_writer = ScriptWriter(script_history)


async def controlled(key: str, coro) -> None:
    """Run a video job or batch, dropping its control once it is over."""
    try:
        await coro
    finally:
        video_controls.close(key)


@dataclass(frozen=True)
class VideoDeps:
    """Everything a video job runs against, resolved at the edge."""

    renderer: VideoRenderer
    speech: SpeechSynth
    slots: TalkingPhotoSlots
    heygen: HeyGenSettings
    nas: NasConfig
    email: EmailSettings


def build_renderer() -> VideoRenderer:
    """Just the renderer, for requests that need HeyGen and nothing else — so a
    missing ElevenLabs or NAS setting can't break them."""
    return HeyGenRenderer(HeyGenSettings.resolve().api_key)


def renderer_for() -> VideoRenderer:
    """``build_renderer``, with a missing key reported as the request's fault."""
    try:
        return build_renderer()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def build_video_deps() -> VideoDeps:
    """Resolve credentials and settings once, here, and build the seams a video
    job runs against.

    A missing key raises here — so it fails the request that asked for the work,
    instead of failing a background job several minutes in.
    """
    heygen = HeyGenSettings.resolve()
    eleven = ElevenLabsSettings.resolve()
    renderer = HeyGenRenderer(heygen.api_key)
    return VideoDeps(
        renderer=renderer,
        speech=ElevenLabsSpeech(eleven.api_key),
        slots=TalkingPhotoSlots(renderer),
        heygen=heygen,
        nas=NasConfig.resolve(),
        email=EmailSettings.resolve(),
    )
