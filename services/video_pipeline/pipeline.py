from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from services.elevenlabs import ElevenLabsTTSConfig
from services.nas import NasConfig, NasService
from services.video_pipeline.image_geometry import clamp_for_render, dimensions
from services.video_pipeline.overlay import burn_cards
from services.video_pipeline.recovery import NotRecoverable
from services.video_pipeline.renderer import VideoRenderer
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.speech import SpeechSynth
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec, VideoJobState

logger = logging.getLogger(__name__)

# smbclient keeps a single process-global SMB session/connection and is NOT
# thread-safe: concurrent uploads (batch mode runs rows in parallel) interleave
# CREATE/WRITE/CLOSE over the one socket, so writes can silently fail to persist
# even though the client returns "OK". Serialize the NAS upload step so only one
# SMB write is ever in flight; renders/polls still run concurrently.
_nas_upload_lock = asyncio.Lock()


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


async def run_video_job(
    *,
    job_id: str,
    spec: VideoJobSpec,
    image_bytes: bytes,
    image_filename: str,
    output_dir: Path,
    jobs_store: VideoJobsStore,
    renderer: VideoRenderer,
    speech: SpeechSynth,
    slots: TalkingPhotoSlots,
    nas_config: NasConfig,
    voice_id: str | None = None,
) -> None:
    try:
        # Persist the spec first thing: if this job later fails on the download or
        # NAS step, recovery needs the title/character/publish_date to re-run.
        await jobs_store.set_spec(job_id, spec)

        resolved_voice_id = spec.voice_id or voice_id
        if not resolved_voice_id:
            raise ValueError(
                f"Missing voice_id for character '{spec.character}' "
                "(provide one or configure the character's voice)"
            )

        # 1. Text-to-speech — content-hash cached so retries / repeat scripts
        # never re-bill the provider. The cache is keyed on everything that can
        # change the audio, and lives here rather than behind the speech seam
        # because a hit is reported to the user as a job status.
        job_dir = output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        audio_path = job_dir / "audio.mp3"

        cache_dir = output_dir / "_audio_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = _tts_cache_key(
            script=spec.script,
            voice_id=resolved_voice_id,
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
            await jobs_store.set_status(job_id, "tts", "Generating audio")
            audio_bytes = await speech.synthesize(
                spec.script,
                config=ElevenLabsTTSConfig(
                    voice_id=resolved_voice_id,
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

        # 2. Upload audio + resolve talking photo
        await jobs_store.set_status(job_id, "uploading", "Uploading audio")
        audio_asset = await renderer.upload_audio(content=audio_bytes, content_type="audio/mpeg")

        if spec.talking_photo_id:
            # Supplied by the caller — a batch acquires one photo up front and
            # puts its id on every row, so rows must not acquire their own.
            talking_photo_id = spec.talking_photo_id
        else:
            await jobs_store.set_status(job_id, "uploading", "Uploading talking photo")
            talking_photo_id = await slots.acquire(
                image_bytes=image_bytes,
                image_filename=image_filename,
                label=f"Job {job_id}",
            )
        await jobs_store.patch_summary(
            job_id,
            audio_asset_id=audio_asset.asset_id,
            image_key=talking_photo_id,
        )

        # 3. Submit the render. Match the source image's aspect ratio so the
        # provider doesn't pad with a white border or fall back to its default
        # 1920x1080 landscape.
        out_w, out_h = spec.width, spec.height
        if (not out_w or not out_h) and image_bytes:
            dims = dimensions(image_bytes)
            if dims:
                out_w, out_h = clamp_for_render(*dims)
                logger.info(
                    "Detected image dims %sx%s, clamped to %sx%s for job %s",
                    dims[0], dims[1], out_w, out_h, job_id,
                )
            else:
                logger.warning("Could not detect image dimensions for job %s; provider defaults apply", job_id)

        await jobs_store.set_status(job_id, "generating", "Submitting render")
        video_id = await renderer.submit(
            photo_id=talking_photo_id,
            audio_asset_id=audio_asset.asset_id,
            motion_prompt=spec.motion_prompt,
            width=out_w,
            height=out_h,
            video_title=spec.video_title,
            correlation_id=job_id,
        )
        await jobs_store.patch_summary(job_id, heygen_video_id=video_id)

        # 4. Wait for it
        await jobs_store.set_status(job_id, "polling", f"Polling render status (video_id={video_id})")
        rendered = await renderer.await_render(video_id=video_id)

        # 5 + 6. Download the render, then upload to NAS. Factored out so the
        # exact same tail can be re-run by recover_video_job for a job that got
        # this far (render finished) but failed on download / NAS.
        await _finalize_video(
            job_id=job_id,
            spec=spec,
            video_url=rendered.video_url,
            job_dir=job_dir,
            jobs_store=jobs_store,
            renderer=renderer,
            nas_config=nas_config,
        )
    except Exception as exc:
        logger.exception("Video job %s failed", job_id)
        await jobs_store.fail(job_id, str(exc), exc=exc)


async def _finalize_video(
    *,
    job_id: str,
    spec: VideoJobSpec,
    video_url: str,
    job_dir: Path,
    jobs_store: VideoJobsStore,
    renderer: VideoRenderer,
    nas_config: NasConfig,
) -> None:
    """Download a finished render to disk and push it to the NAS, then mark the
    job completed. Shared by the main pipeline and recovery.

    The render URL + video id are recorded BEFORE downloading so that if the
    download still fails, the persisted job keeps a handle to re-fetch the
    finished render instead of losing it. The renderer's download itself retries
    with resume, so a transient CDN connection drop no longer fails the job.
    """
    # 5. Download to local storage (URL expires in ~7 days).
    video_path = job_dir / "video.mp4"
    await jobs_store.patch_summary(
        job_id,
        video_url=video_url,
        video_path=str(video_path),
    )
    await jobs_store.set_status(job_id, "downloading", "Downloading rendered video")
    await renderer.download(video_url=video_url, dest_path=video_path)

    from datetime import date as _date
    publish_date = spec.publish_date or _date.today().strftime("%d-%m-%Y")

    # 5b. Burn the branded cards and/or music bed onto the render before upload.
    # The sign is spec.video_title (already Devanagari); the date drives the top
    # card. A single job may opt out of either; with both off the raw render is
    # uploaded untouched. Batch and recovery keep the defaults (both on).
    if spec.include_overlay or spec.include_music:
        bits = [b for b, on in (("overlay cards", spec.include_overlay),
                                ("music", spec.include_music)) if on]
        await jobs_store.set_status(job_id, "nas_upload", "Adding " + " & ".join(bits))
        final_path = job_dir / "video_carded.mp4"
        await asyncio.to_thread(
            burn_cards, video_path, final_path, spec.video_title, publish_date,
            overlay=spec.include_overlay, music=spec.include_music,
        )
    else:
        final_path = video_path

    # 6. Upload to NAS
    await jobs_store.set_status(job_id, "nas_upload", "Uploading to NAS")
    # US-character content lands in its own NAS folder, when configured;
    # everything else uses the default root.
    target = nas_config.for_character(spec.character)
    if target is not nas_config:
        logger.info("Job %s: US character → NAS root '%s'", job_id, target.root_path)
    nas = NasService(target)
    async with _nas_upload_lock:
        nas_path = await asyncio.to_thread(
            nas.upload_video, publish_date, spec.video_title, str(final_path)
        )
    await jobs_store.patch_summary(job_id, nas_path=nas_path)

    await jobs_store.complete(job_id)


Launch = Callable[..., Any]


async def start_recovery(
    *,
    job_id: str,
    jobs_store: VideoJobsStore,
    output_dir: Path,
    renderer: VideoRenderer,
    nas_config: NasConfig,
    launch: Launch,
) -> None:
    """Claim one job for Recovery, then hand its run to ``launch`` (called as
    ``launch(coro, name=...)``, i.e. ``spawn``).

    Raises NotRecoverable — and launches nothing — if the job may not be
    recovered now, including when a recovery of it is already under way.
    """
    state = await jobs_store.claim_recovery(job_id)
    coro = _recover_claimed(state, jobs_store=jobs_store, output_dir=output_dir,
                            renderer=renderer, nas_config=nas_config)
    try:
        launch(coro, name=f"video-recover:{job_id}")
    except BaseException as exc:
        # Nothing will run the recovery, so release the claim — or the job
        # sits in 'recovering' and refuses every retry until a restart.
        coro.close()
        await jobs_store.fail(job_id, f"Recovery could not be started: {exc}", exc=exc)
        raise


async def start_recovery_of_failed(
    *,
    jobs_store: VideoJobsStore,
    output_dir: Path,
    renderer: VideoRenderer,
    nas_config: NasConfig,
    launch: Launch,
) -> list[str]:
    """Start a recovery for every recoverable job; return the ids started.

    A job that stops being recoverable between the listing and its claim (say,
    a single recover request got there first) is passed by, not raised.
    """
    started: list[str] = []
    for job_id in await jobs_store.list_recoverable():
        try:
            await start_recovery(
                job_id=job_id, jobs_store=jobs_store, output_dir=output_dir,
                renderer=renderer, nas_config=nas_config, launch=launch,
            )
        except NotRecoverable as exc:
            logger.info("Skipping recovery of video job %s: %s", job_id, exc.reason)
            continue
        started.append(job_id)
    return started


async def recover_video_job(
    *,
    job_id: str,
    jobs_store: VideoJobsStore,
    output_dir: Path,
    renderer: VideoRenderer,
    nas_config: NasConfig,
) -> None:
    """Claim a job for Recovery and run it here, to the end. Raises
    NotRecoverable if the job may not be recovered now."""
    state = await jobs_store.claim_recovery(job_id)
    await _recover_claimed(state, jobs_store=jobs_store, output_dir=output_dir,
                           renderer=renderer, nas_config=nas_config)


async def _recover_claimed(
    state: VideoJobState,
    *,
    jobs_store: VideoJobsStore,
    output_dir: Path,
    renderer: VideoRenderer,
    nas_config: NasConfig,
) -> None:
    """Re-run the download + NAS-upload tail for a job whose render finished but
    which failed afterward (transient download/NAS error that exhausted retries,
    or a process crash mid-download).

    The job has been claimed, so it is known to carry a video id and its spec.
    The stored video_url may have expired, so we re-fetch a fresh one.
    """
    job_id = state.job_id
    video_id = state.summary.heygen_video_id
    try:
        # Re-fetch: the stored URL expires (~7 days) and this also confirms the
        # render is still available. await_render returns at once if done.
        await jobs_store.set_status(job_id, "polling", f"Re-fetching render (video_id={video_id})")
        rendered = await renderer.await_render(video_id=video_id)

        job_dir = output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        await _finalize_video(
            job_id=job_id,
            spec=state.spec,
            video_url=rendered.video_url,
            job_dir=job_dir,
            jobs_store=jobs_store,
            renderer=renderer,
            nas_config=nas_config,
        )
        logger.info("Recovered video job %s", job_id)
    except (Exception, asyncio.CancelledError) as exc:
        # Cancellation too: a job left in 'downloading' is not 'failed', so it
        # would refuse every later recovery until a restart settled it.
        logger.exception("Recovery of video job %s failed", job_id)
        await jobs_store.fail(job_id, str(exc), exc=exc)
        raise
