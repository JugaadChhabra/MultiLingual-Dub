"""Compilations: the pane's way through to the Comp engine.

A proxy, not a pass-through: only the routes the pane uses are forwarded. The
engine's own settings (library folders, encoder, how many run together) are
set once on the Mac mini and are not the browser's to change, so its POST
/api/config and /api/queue/parallel are deliberately not reachable from here.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from services import comp_engine
from services.comp_engine import CompEngineSettings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/comps")

ENGINE_DOWN = (
    "The Comp engine isn't answering. On the Mac mini, check it is running: "
    "launchctl print gui/$(id -u)/com.contenthub.compcreator"
)

GET_ROUTES = {"config", "rhymes", "info", "detect", "queue"}
POST_ROUTES = {"queue/add", "queue/cancel", "queue/move", "queue/clear", "queue/refile"}

# Headers a video player needs to seek and to save the file under its name.
STREAM_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges",
                  "content-disposition", "last-modified", "cache-control")


def _down(exc: Exception) -> JSONResponse:
    logger.warning("Comp engine unreachable: %s", exc)
    return JSONResponse({"error": ENGINE_DOWN, "engine": False}, status_code=503)


async def _forward_json(method: str, path: str, request: Request) -> Response:
    settings = CompEngineSettings.resolve()
    body = await request.body() if method == "POST" else None
    try:
        async with comp_engine.client(settings) as c:
            r = await c.request(method, path, params=dict(request.query_params), content=body,
                                headers={"Content-Type": "application/json"} if body else None)
    except httpx.HTTPError as exc:
        return _down(exc)
    return Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@router.get("/api/{route:path}")
async def engine_get(route: str, request: Request) -> Response:
    if route not in GET_ROUTES:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _forward_json("GET", f"/api/{route}", request)


@router.post("/api/{route:path}")
async def engine_post(route: str, request: Request) -> Response:
    if route not in POST_ROUTES:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _forward_json("POST", f"/api/{route}", request)


async def _stream(path: str, request: Request) -> Response:
    """Thumbnails and comps, streamed. Range goes through untouched, so the
    player can seek a two-hour comp without the container buffering it."""
    settings = CompEngineSettings.resolve()
    c = comp_engine.client(settings, read_timeout=None)
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
    try:
        upstream = await c.send(c.build_request("GET", path, params=dict(request.query_params),
                                                headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await c.aclose()
        return _down(exc)

    async def close() -> None:
        await upstream.aclose()
        await c.aclose()

    if upstream.status_code >= 400:
        await close()
        return Response(status_code=upstream.status_code)
    return StreamingResponse(
        # aiter_bytes, not aiter_raw: it also serves a body already read. The
        # engine never compresses, so the forwarded Content-Length still holds.
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() in STREAM_HEADERS},
        background=BackgroundTask(close),
    )


@router.get("/thumb")
async def thumb(request: Request) -> Response:
    return await _stream("/thumb", request)


@router.get("/video")
async def video(request: Request) -> Response:
    return await _stream("/video", request)
