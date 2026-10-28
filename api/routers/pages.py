"""The pages: one HTML shell, served at each section's own linkable path."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter()


# The HTML must always be revalidated. Without an explicit Cache-Control browsers
# apply heuristic freshness and can serve a stale page against freshly deployed
# JS — the markup then lacks the ids the new script expects and the page dies on
# load. The ?v= query on the asset tags only helps if the HTML carrying it is
# current, so the pages say no-cache and /static assets stay cacheable.
NO_HTML_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@router.get("/")
def index() -> FileResponse:
    index_path = Path("./index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path, headers=NO_HTML_CACHE)


@router.get("/videogen")
def videogen_page() -> FileResponse:
    """Both pipelines live in one app now. This stays a real route so the Video
    section can be linked and bookmarked directly — the shell reads the path and
    opens it."""
    return index()


@router.get("/story")
def story_page() -> FileResponse:
    """The Story section's own linkable path, like /videogen."""
    return index()


@router.get("/library")
def library_page() -> FileResponse:
    """The video library's own linkable path, like /videogen."""
    return index()


@router.get("/heygen", include_in_schema=False)
def heygen_page_legacy() -> RedirectResponse:
    """The old name. Kept as a redirect rather than deleted: the video editor has
    this bookmarked, and a 308 renames the URL without costing them anything."""
    return RedirectResponse("/videogen", status_code=308)
