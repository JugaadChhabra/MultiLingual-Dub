"""The video library (every HeyGen render, matched to its job) and Recovery.

Included before the video router: ``/video/heygen/library`` would otherwise be
taken by ``/video/heygen/{job_id}`` as a job id.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api import shared
from api.background import spawn
from api.models import FileRenderRequest
from api.utils import to_output_url
from services.runtime_config import MissingSettingError
from services.video_pipeline import NotRecoverable, start_recovery, start_recovery_of_failed
from services.video_pipeline.intake import scrub_title
from services.video_pipeline.library import page_of
from services.video_pipeline.library_service import Download, NoMedia, sniff_image_type
from services.video_pipeline.pipeline import start_filing_render
from services.video_pipeline.types import VideoJobSpec, VideoJobState

router = APIRouter()


# The rules live in services/video_pipeline/library.py and the caching in
# library_service.py; these only translate to and from HTTP.

# A render's thumbnail never changes, so the browser may keep it for good.
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _csv(raw: str | None) -> set[str]:
    return set((raw or "").split(",")) - {""}


async def _record_recovery(state: VideoJobState) -> None:
    """A recovered job's batch row, if it has one, is done now too."""
    video_path = state.summary.video_path
    await shared.video_batch_jobs_store.row_recovered(
        state.job_id,
        nas_path=state.summary.nas_path,
        video_local_url=to_output_url(video_path, shared.OUTPUT_DIR) if video_path else None,
    )


@router.get("/video/heygen/library")
async def get_video_library(
    refresh: bool = False,
    state: str | None = None,
    q: str | None = None,
    ids: str | None = None,
    offset: int = 0,
    limit: int = 50,
):
    """Every render on the HeyGen account, newest first, each with what can be
    done with it. ``counts`` covers the whole library; ``items`` the filtered
    page. ``ids`` lets the page ask after the renders it sent, wherever they are."""
    renderer = shared.renderer_for()
    lib = await shared.video_library.library(renderer, refresh=refresh)
    page = page_of(lib.entries, states=_csv(state), ids=_csv(ids), query=q or "",
                   offset=offset, limit=min(limit, 200))
    return {
        "items": [e.to_dict() for e in page.items],
        "total": page.total,
        "counts": page.counts,
        "listing_error": lib.listing_error,
    }


@router.post("/video/heygen/library/{video_id}/send-to-nas", status_code=202)
async def send_render_to_nas(video_id: str, details: FileRenderRequest | None = None):
    """File a finished render to the NAS without re-rendering it. A render one
    of our jobs made is recovered from that job's own details; one we know
    nothing about needs ``details`` (title, publish date, character)."""
    try:
        deps = shared.build_video_deps()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entry = await shared.video_library.entry(deps.renderer, video_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such render on HeyGen or in our jobs")

    if entry.state == "sendable":
        try:
            await start_recovery(
                job_id=entry.job_id, jobs_store=shared.video_jobs_store, output_dir=shared.VIDEO_OUTPUT_DIR,
                renderer=deps.renderer, nas_config=deps.nas, launch=spawn,
                on_recovered=_record_recovery,
            )
        except NotRecoverable as exc:
            raise HTTPException(status_code=409, detail=exc.reason) from exc
        return {"video_id": video_id, "job_id": entry.job_id, "status": "recovering"}

    if entry.state == "needs_details":
        if details is None:
            raise HTTPException(status_code=422, detail="This render has no title or date on file; send them with it")
        title = scrub_title(details.title)
        if not title:
            raise HTTPException(status_code=422, detail="title is empty once unsafe characters are removed")
        spec = VideoJobSpec(
            script="", video_title=title, character=details.character.strip().lower() or "indian",
            publish_date=details.publish_date, include_overlay=details.include_overlay,
            include_music=details.include_music,
        )
        job_id = await start_filing_render(
            video_id=video_id, spec=spec, jobs_store=shared.video_jobs_store, output_dir=shared.VIDEO_OUTPUT_DIR,
            renderer=deps.renderer, nas_config=deps.nas, launch=spawn,
        )
        return {"video_id": video_id, "job_id": job_id, "status": "recovering"}

    refusal = {
        "on_nas": f"Already on the NAS at {entry.nas_path}",
        "in_progress": "Already being worked on",
        "rendering": "Still rendering on HeyGen; try again when it finishes",
    }.get(entry.state, entry.reason)
    raise HTTPException(status_code=409, detail=refusal)


async def _download(video_id: str) -> Download:
    renderer = shared.renderer_for()
    try:
        return await shared.video_library.download(renderer, video_id)
    except NoMedia as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch the render from HeyGen: {exc}") from exc


@router.post("/video/heygen/library/{video_id}/prepare-download")
async def prepare_render_download(video_id: str):
    """Make sure the video is on this server, then say where to fetch it. Two
    steps so the page can report a failure, then let the browser stream the
    file to disk instead of holding 30 MB in memory."""
    await _download(video_id)
    return {"url": f"/video/heygen/library/{video_id}/download"}


@router.get("/video/heygen/library/{video_id}/download")
async def download_render(video_id: str):
    """The video as a file: what a job of ours filed when we have it, otherwise
    the raw render, fetched from HeyGen once and kept."""
    dl = await _download(video_id)
    return FileResponse(dl.path, media_type="video/mp4", filename=dl.filename)


@router.get("/video/heygen/library/{video_id}/thumbnail")
async def render_thumbnail(video_id: str):
    """The render's thumbnail: fetched from HeyGen the first time anyone asks,
    from disk after that, and from the browser's own cache after that."""
    renderer = shared.renderer_for()
    try:
        path = await shared.video_library.thumbnail(renderer, video_id)
    except (NoMedia, ValueError) as exc:
        # Not cached: a render still processing will have one later.
        raise HTTPException(status_code=404, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch the thumbnail from HeyGen: {exc}",
                            headers={"Cache-Control": "no-store"}) from exc
    return FileResponse(path, media_type=sniff_image_type(path), headers=IMMUTABLE)


@router.get("/video/heygen/jobs/recoverable")
async def list_recoverable_heygen_jobs():
    """Failed jobs whose HeyGen render actually finished — re-runnable via recover."""
    ids = await shared.video_jobs_store.list_recoverable()
    return {"recoverable": ids, "count": len(ids)}


@router.post("/video/heygen/{job_id}/recover", status_code=202)
async def recover_heygen_video_job(job_id: str):
    """Re-download a finished-but-failed render and push it to the NAS. Recovers
    a job that died on the download/NAS step (its HeyGen render is intact)."""
    try:
        deps = shared.build_video_deps()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        await start_recovery(
            job_id=job_id,
            jobs_store=shared.video_jobs_store,
            output_dir=shared.VIDEO_OUTPUT_DIR,
            renderer=deps.renderer,
            nas_config=deps.nas,
            launch=spawn,
            on_recovered=_record_recovery,
        )
    except NotRecoverable as exc:
        status = 404 if exc.kind == "not_found" else 409
        raise HTTPException(status_code=status, detail=exc.reason) from exc
    return {"job_id": job_id, "status": "recovering"}


@router.post("/video/heygen/recover-failed", status_code=202)
async def recover_all_failed_heygen_jobs():
    """Recover every failed job whose render finished on HeyGen, in one shot.
    Jobs already being recovered are passed by."""
    try:
        deps = shared.build_video_deps()
    except MissingSettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ids = await start_recovery_of_failed(
        jobs_store=shared.video_jobs_store,
        output_dir=shared.VIDEO_OUTPUT_DIR,
        renderer=deps.renderer,
        nas_config=deps.nas,
        launch=spawn,
        on_recovered=_record_recovery,
    )
    started = set(ids)
    video_ids = [j.summary.heygen_video_id for j in await shared.video_jobs_store.snapshot() if j.job_id in started]
    return {"status": "recovering", "job_ids": ids, "video_ids": video_ids, "count": len(ids)}
