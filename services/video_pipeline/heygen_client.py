from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass

import httpx

from services.runtime_config import read_setting, require

logger = logging.getLogger(__name__)

HEYGEN_API_BASE = "https://api.heygen.com"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=30.0)
UPLOAD_TIMEOUT = httpx.Timeout(connect=60.0, read=300.0, write=300.0, pool=30.0)

# HeyGen's edge routinely reaps a pooled keep-alive connection mid-flight, which
# httpx surfaces as RemoteProtocolError ("Server disconnected without sending a
# response"). That is a httpx.TransportError but NOT a ConnectError/TimeoutException,
# so the old per-call guards (which caught only those two) let it through and failed
# the whole job. Catch the whole TransportError family — connect errors, read/write
# timeouts, pool timeouts, and remote disconnects are all transient — and retry with
# a fresh connection so the poisoned pooled socket is discarded.
_HEYGEN_RETRIES = 4

# Test seam. Left None in production so every attempt opens a real connection;
# tests set an httpx.MockTransport here to drive the retry / adoption / resume
# paths without a network. Module-level rather than a parameter because it has
# to reach _send through a dozen call sites that have no business knowing about
# transports.
_transport: httpx.BaseTransport | None = None


def _send(
    method: str,
    url: str,
    *,
    timeout: httpx.Timeout,
    what: str,
    retries: int = _HEYGEN_RETRIES,
    **kwargs,
) -> httpx.Response:
    """Issue an httpx request, retrying transient transport failures with capped
    exponential backoff. A fresh Client (and thus connection) is opened per attempt.
    HTTP status codes are returned untouched for the caller to handle."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, transport=_transport) as client:
                return client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            delay = min(2 ** attempt, 20)
            logger.warning(
                "HeyGen %s transient transport error (attempt %d/%d): %s — retrying in %ds",
                what, attempt, retries, exc, delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # loop only exits via return or break-with-exc
    raise last_exc


# Maps a character handle to the key holding its ElevenLabs voice id.
CHARACTER_VOICE_ENV = {
    "indian": "ISHWARI_VOICE_ID",
    "us": "US_VOICE_ID",
}
DEFAULT_CHARACTER = "indian"

_DEFAULT_BATCH_CONCURRENCY = 4


@dataclass(frozen=True)
class HeyGenSettings:
    api_key: str
    # Voice id per character handle. Empty for a character whose voice was never
    # configured — a job may still supply one explicitly on its spec.
    character_voice_ids: dict[str, str]
    batch_concurrency: int

    REQUIRED = ("HEYGEN_ISHWARI",)

    @classmethod
    def resolve(cls) -> HeyGenSettings:
        values = require(cls.REQUIRED)
        raw = read_setting("HEYGEN_BATCH_CONCURRENCY")
        try:
            concurrency = int(raw) if raw else _DEFAULT_BATCH_CONCURRENCY
        except (TypeError, ValueError):
            concurrency = _DEFAULT_BATCH_CONCURRENCY
        return cls(
            api_key=values["HEYGEN_ISHWARI"],
            character_voice_ids={
                character: read_setting(key)
                for character, key in CHARACTER_VOICE_ENV.items()
            },
            batch_concurrency=max(1, concurrency),
        )

    def voice_for_character(self, character: str | None) -> str | None:
        handle = (character or DEFAULT_CHARACTER).lower()
        if handle not in self.character_voice_ids:
            handle = DEFAULT_CHARACTER
        return self.character_voice_ids.get(handle) or None


@dataclass(frozen=True)
class UploadResult:
    asset_id: str
    url: str | None


def _data(resp: httpx.Response) -> dict:
    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else (body if isinstance(body, dict) else {})


def _error(resp: httpx.Response) -> tuple[str, str]:
    """(code, message) from a v3 error body ``{"error": {"code", "message"}}``."""
    try:
        body = resp.json()
    except Exception:
        return "", resp.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or ""), str(err.get("message") or "")
    if isinstance(body, dict):
        return str(body.get("code") or ""), str(body.get("message") or "")
    return "", ""


def _raise_for_status(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 400:
        logger.error("HeyGen %s failed: %s %s", what, resp.status_code, resp.text[:500])
    resp.raise_for_status()


_EXT_FOR_TYPE = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "video/mp4": "mp4", "video/webm": "webm",
}


def upload_asset(*, api_key: str, content: bytes, content_type: str) -> UploadResult:
    """Upload a file to HeyGen's asset store (v3: one multipart ``file`` field)."""
    ext = _EXT_FOR_TYPE.get(content_type.lower(), "bin")
    resp = _send(
        "POST", f"{HEYGEN_API_BASE}/v3/assets", timeout=UPLOAD_TIMEOUT,
        what="asset upload", headers={"X-Api-Key": api_key},
        files={"file": (f"upload.{ext}", content, content_type)},
    )
    _raise_for_status(resp, "asset upload")
    data = _data(resp)
    asset_id = data.get("asset_id") or data.get("id")
    if not asset_id:
        raise RuntimeError(f"HeyGen asset upload returned no asset_id: {resp.text[:300]}")
    return UploadResult(asset_id=str(asset_id), url=data.get("url"))


def _paginate(*, api_key: str, path: str, params: dict, what: str) -> list[dict]:
    """Every item of a v3 list endpoint, following ``next_token``."""
    items: list[dict] = []
    token: str | None = None
    while True:
        query = dict(params)
        if token:
            query["token"] = token
        resp = _send(
            "GET", f"{HEYGEN_API_BASE}{path}", timeout=DEFAULT_TIMEOUT,
            what=what, headers={"X-Api-Key": api_key}, params=query,
        )
        _raise_for_status(resp, what)
        body = resp.json()
        page = body.get("data") or []
        items.extend(i for i in page if isinstance(i, dict))
        token = body.get("next_token")
        if not token or not page or body.get("has_more") is False:
            return items


def _photo_looks(*, api_key: str) -> list[dict]:
    """The user's own photo-avatar looks. Only photo avatars — never a digital
    twin or studio avatar — so the cap sweep can't delete anything else."""
    return _paginate(
        api_key=api_key, path="/v3/avatars/looks",
        params={"ownership": "private", "avatar_type": "photo_avatar", "limit": 50},
        what="avatar looks list",
    )


def list_talking_photos(*, api_key: str) -> list[dict]:
    """List the user's own photo avatars (capped at 3 by HeyGen), one entry per
    group, using the group's first usable look as the talking_photo_id that
    /v3/videos takes as ``avatar_id``."""
    looks = _photo_looks(api_key=api_key)
    groups = {
        str(g["id"]): g
        for g in _paginate(
            api_key=api_key, path="/v3/avatars",
            params={"ownership": "private", "limit": 50}, what="avatar groups list",
        )
        if g.get("id")
    }
    out: list[dict] = []
    seen: set[str] = set()
    for look in looks:
        gid = str(look.get("group_id") or "")
        if not look.get("id") or not gid or gid in seen:
            continue
        if look.get("status") not in (None, "completed"):
            continue
        seen.add(gid)
        group = groups.get(gid, {})
        out.append({
            "talking_photo_id": str(look["id"]),
            "group_id": gid,
            "name": group.get("name") or look.get("name") or "",
            "preview_url": look.get("preview_image_url") or group.get("preview_image_url"),
            "created_at": group.get("created_at"),
        })
    return out


def delete_avatar_group(*, api_key: str, group_id: str) -> None:
    """Delete an entire photo-avatar group. This is what frees up a slot
    against HeyGen's 3-photo-avatar cap; deleting individual looks does not."""
    resp = _send(
        "DELETE", f"{HEYGEN_API_BASE}/v3/avatars/{group_id}",
        timeout=DEFAULT_TIMEOUT, what="avatar group delete", headers={"X-Api-Key": api_key},
    )
    _raise_for_status(resp, "avatar group delete")


def _list_avatar_group_ids(*, api_key: str) -> list[str]:
    """Every group that holds one of the user's photo-avatar looks — including
    looks still processing or failed, which still occupy a slot."""
    ids: list[str] = []
    for look in _photo_looks(api_key=api_key):
        gid = str(look.get("group_id") or "")
        if gid and gid not in ids:
            ids.append(gid)
    return ids


def clear_talking_photos(*, api_key: str) -> int:
    """Delete ALL user-owned photo-avatar groups so the next upload starts with
    free slots against HeyGen's 3-photo-avatar cap. Best-effort: a single group's
    delete failure is logged and skipped rather than aborting the sweep. Returns
    the number of groups successfully deleted. Safe only when no other run is
    mid-render against these photos."""
    group_ids = _list_avatar_group_ids(api_key=api_key)
    deleted = 0
    for gid in group_ids:
        try:
            delete_avatar_group(api_key=api_key, group_id=gid)
            deleted += 1
        except Exception as exc:
            logger.warning("clear_talking_photos: failed to delete group %s: %s", gid, exc)
    if group_ids:
        logger.info("clear_talking_photos: deleted %d/%d photo-avatar groups", deleted, len(group_ids))
    return deleted


# v1's numeric code for the photo-avatar cap. v3 reports errors with string
# codes; _is_photo_cap_error matches either.
QUOTA_EXCEEDED_CODE = 401028
_CAP_WORDS = re.compile(r"limit|quota|maximum|max_|exceed|cap", re.I)


def _is_photo_cap_error(resp: httpx.Response) -> bool:
    if resp.status_code < 400 or resp.status_code == 429 or resp.status_code >= 500:
        return False
    code, message = _error(resp)
    if code == str(QUOTA_EXCEEDED_CODE):
        return True
    if code in ("invalid_parameter", "unauthorized", "rate_limit_exceeded", "request_in_progress"):
        return False
    return bool(_CAP_WORDS.search(f"{code} {message}"))


def _post_photo_avatar(*, api_key: str, asset_id: str, name: str) -> httpx.Response:
    return _send(
        "POST", f"{HEYGEN_API_BASE}/v3/avatars", timeout=DEFAULT_TIMEOUT,
        what="photo avatar create", headers={"X-Api-Key": api_key},
        json={"type": "photo", "name": name, "file": {"type": "asset_id", "asset_id": asset_id}},
    )


def _wait_for_look(
    *, api_key: str, look_id: str, interval_seconds: float = 4.0, timeout_seconds: float = 600.0
) -> None:
    """Photo-avatar training is asynchronous in v3: /v3/videos rejects a look
    that is still processing, so block until it is completed."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        resp = _send(
            "GET", f"{HEYGEN_API_BASE}/v3/avatars/looks/{look_id}", timeout=DEFAULT_TIMEOUT,
            what="avatar look status", headers={"X-Api-Key": api_key},
        )
        _raise_for_status(resp, "avatar look status")
        look = _data(resp)
        status = look.get("status")
        if status in (None, "completed"):
            return
        if status == "failed":
            err = look.get("error") or {}
            detail = err.get("message") or err.get("code") if isinstance(err, dict) else err
            raise RuntimeError(f"HeyGen photo avatar failed: {detail or 'unknown error'}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"HeyGen photo avatar {look_id} still {status} after {timeout_seconds}s")
        time.sleep(interval_seconds)


def upload_talking_photo(*, api_key: str, content: bytes, content_type: str) -> str:
    """Create a photo avatar from an image and return its look id (the
    ``avatar_id`` for /v3/videos). If the per-account cap is hit, delete the
    oldest user-owned photo avatar and retry once."""
    asset = upload_asset(api_key=api_key, content=content, content_type=content_type)
    name = f"Content Hub {time.strftime('%Y-%m-%d %H:%M:%S')}"
    resp = _post_photo_avatar(api_key=api_key, asset_id=asset.asset_id, name=name)
    if _is_photo_cap_error(resp):
        logger.warning("HeyGen photo avatar cap hit (%s); rotating oldest photo avatar", resp.text[:200])
        existing = list_talking_photos(api_key=api_key)
        if not existing:
            raise RuntimeError(f"photo avatar create hit cap but no existing avatars to rotate: {resp.text[:300]}")
        existing.sort(key=lambda x: x.get("created_at") or 0)
        delete_avatar_group(api_key=api_key, group_id=existing[0]["group_id"])
        resp = _post_photo_avatar(api_key=api_key, asset_id=asset.asset_id, name=name)

    _raise_for_status(resp, "photo avatar create")
    data = _data(resp)
    item = data.get("avatar_item") or {}
    look_id = item.get("id") if isinstance(item, dict) else None
    if not look_id:
        raise RuntimeError(f"photo avatar create returned no look id: {resp.text[:300]}")
    _wait_for_look(api_key=api_key, look_id=str(look_id))
    return str(look_id)


# /v3/videos takes an aspect ratio, not pixel dimensions.
_ASPECT_RATIOS = {"16:9": 16 / 9, "9:16": 9 / 16, "4:5": 4 / 5, "5:4": 5 / 4, "1:1": 1.0}


def _aspect_ratio(width: int | None, height: int | None) -> str:
    """The v3 aspect ratio closest to width x height, or 'auto' (match the
    source photo) when nothing is within 3% — padding to the wrong ratio is the
    white-border bug the explicit dimensions were added to avoid."""
    if not width or not height:
        return "auto"
    ratio = width / height
    name, value = min(_ASPECT_RATIOS.items(), key=lambda kv: abs(math.log(ratio / kv[1])))
    return name if abs(math.log(ratio / value)) <= math.log(1.03) else "auto"


def _resolution(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    return "1080p" if min(width, height) >= 1080 else "720p"


_SUBMIT_PASSES = 4


def create_avatar_iv_video(
    *,
    api_key: str,
    talking_photo_id: str,
    audio_asset_id: str,
    motion_prompt: str | None,
    width: int | None = None,
    height: int | None = None,
    video_title: str,
    callback_id: str | None,
) -> str:
    body: dict = {
        "type": "avatar",
        "avatar_id": talking_photo_id,
        "audio_asset_id": audio_asset_id,
        "title": video_title,
        "engine": {"type": "avatar_iv"},
        "aspect_ratio": _aspect_ratio(width, height),
    }
    resolution = _resolution(width, height)
    if resolution:
        body["resolution"] = resolution
    if motion_prompt:
        body["motion_prompt"] = motion_prompt
    if callback_id:
        body["callback_id"] = callback_id

    # A video create is billed, and a lost response (ReadTimeout /
    # RemoteProtocolError after the request reached HeyGen) used to mean either
    # a duplicate render on resubmit or a lookup dance to adopt the orphan — the
    # "12 rows in, 15 renders out" bug. v3 takes an Idempotency-Key: a replay
    # within 24h returns the original response instead of creating a second
    # render. The key is fixed for this call's retries but fresh per call, so a
    # job that deliberately re-renders later does not get its old video back.
    idem = f"{callback_id or 'render'}.{uuid.uuid4().hex[:12]}"
    headers = {"X-Api-Key": api_key, "Idempotency-Key": idem}

    last_exc: Exception | None = None
    for attempt in range(1, _SUBMIT_PASSES + 1):
        try:
            resp = _send(
                "POST", f"{HEYGEN_API_BASE}/v3/videos", timeout=DEFAULT_TIMEOUT,
                what="video create", retries=1, headers=headers, json=body,
            )
        except httpx.TransportError as exc:
            last_exc = exc
            logger.warning(
                "video create transport error (pass %d/%d): %s — replaying with the same "
                "Idempotency-Key", attempt, _SUBMIT_PASSES, exc,
            )
            time.sleep(min(2 ** attempt, 20))
            continue
        # The original request is still being processed: wait, then replay.
        if resp.status_code == 409 and _error(resp)[0] == "request_in_progress":
            last_exc = httpx.HTTPStatusError("request_in_progress", request=resp.request, response=resp)
            time.sleep(min(2 ** attempt, 20))
            continue
        _raise_for_status(resp, "video create")
        vid = _data(resp).get("video_id")
        if not vid:
            raise RuntimeError(f"HeyGen response missing video_id: {resp.text[:300]}")
        return str(vid)
    assert last_exc is not None
    raise last_exc


class HeyGenRenderFailed(RuntimeError):
    """HeyGen reports the render itself failed: there is no video to fetch."""


def list_videos(*, api_key: str, known: frozenset[str] = frozenset(), page_size: int = 100) -> list[dict]:
    """Every render on the account, newest first, following the page token.
    Stops early at a page whose ids are all in ``known`` — everything older is
    something the caller already has."""
    headers = {"X-Api-Key": api_key}
    videos: list[dict] = []
    token: str | None = None
    while True:
        params: dict = {"limit": min(max(page_size, 1), 100)}
        if token:
            params["token"] = token
        resp = _send(
            "GET", f"{HEYGEN_API_BASE}/v3/videos", timeout=DEFAULT_TIMEOUT,
            what="video list", headers=headers, params=params,
        )
        _raise_for_status(resp, "video list")
        body = resp.json()
        page = [v for v in (body.get("data") or []) if isinstance(v, dict)]
        videos.extend(page)
        token = body.get("next_token") if body.get("has_more", True) else None
        ids = {str(v.get("id") or v.get("video_id") or "") for v in page}
        if not token or not page or (known and ids <= known):
            return videos


def get_video_status(*, api_key: str, video_id: str) -> dict:
    resp = _send(
        "GET", f"{HEYGEN_API_BASE}/v3/videos/{video_id}", timeout=DEFAULT_TIMEOUT,
        what="video status", headers={"X-Api-Key": api_key},
    )
    _raise_for_status(resp, "video status")
    return _data(resp)


async def poll_until_done(
    *,
    api_key: str,
    video_id: str,
    interval_seconds: float = 6.0,
    timeout_seconds: float = 1500.0,
    max_network_retries: int = 5,
) -> dict:
    elapsed = 0.0
    network_failures = 0
    while elapsed < timeout_seconds:
        try:
            data = await asyncio.to_thread(get_video_status, api_key=api_key, video_id=video_id)
            network_failures = 0
        except httpx.TransportError as exc:
            network_failures += 1
            if network_failures > max_network_retries:
                raise
            logger.warning(
                "HeyGen status poll network error (attempt %d/%d): %s — retrying",
                network_failures, max_network_retries, exc,
            )
            await asyncio.sleep(interval_seconds)
            elapsed += interval_seconds
            continue
        status = data.get("status")
        if status == "completed":
            return data
        if status == "failed":
            err = (data.get("failure_message") or data.get("failure_code")
                   or data.get("error") or "unknown error")
            raise HeyGenRenderFailed(f"HeyGen render failed: {err}")
        await asyncio.sleep(interval_seconds)
        elapsed += interval_seconds
    raise TimeoutError(f"HeyGen render did not complete within {timeout_seconds}s")


DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
_DOWNLOAD_RETRIES = 6


def _expected_size(resp: httpx.Response, already_have: int) -> int | None:
    """Total file size from a (possibly partial) response, or None if unknown.

    For a 206 response Content-Length is the size of the *remaining* range, so
    the total is what we already had plus the body length."""
    cl = resp.headers.get("content-length")
    if cl is None:
        return None
    body = int(cl)
    return already_have + body if resp.status_code == 206 else body


def _range_total(resp: httpx.Response) -> int | None:
    """The full size a 416 reports in its ``Content-Range: bytes */N``."""
    _, _, total = (resp.headers.get("content-range") or "").rpartition("/")
    return int(total) if total.isdigit() else None


def download_video(url: str, dest_path: str) -> int:
    """Stream a rendered video to disk, surviving mid-transfer connection drops.

    HeyGen's CDN routinely closes the connection before the full body arrives
    (`RemoteProtocolError: peer closed connection ... received X, expected Y`).
    A single-shot stream turns that transient blip into a permanently failed —
    and credit-wasting — job. So we retry, resuming from the bytes already on
    disk via HTTP Range requests, and only return once the file size matches the
    server's advertised Content-Length. A short read that the CDN reports as a
    "complete" 200 (no Content-Length) still gets a size check on the next pass.
    """
    last_exc: Exception | None = None
    expected: int | None = None

    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        have = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.Client(
                timeout=DOWNLOAD_TIMEOUT, follow_redirects=True, transport=_transport
            ) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    # Nothing after byte `have`. If that is the whole render the
                    # file is already done — recovery re-downloads into a folder
                    # where a job that died on the NAS step left it complete.
                    # Otherwise what's on disk is not a prefix of this render.
                    if have and resp.status_code == 416:
                        total = _range_total(resp)
                        if total == have:
                            return have
                        logger.warning(
                            "Video on disk (%d bytes) does not match the render (%s bytes); "
                            "downloading it again", have, total if total is not None else "?",
                        )
                        os.remove(dest_path)
                        continue
                    # Server ignored our Range and is resending the whole file:
                    # truncate so we don't append a duplicate prefix.
                    if have and resp.status_code == 200:
                        have = 0
                    elif resp.status_code not in (200, 206):
                        resp.raise_for_status()
                    expected = _expected_size(resp, have) or expected
                    mode = "ab" if have else "wb"
                    with open(dest_path, mode) as f:
                        for chunk in resp.iter_bytes():
                            if chunk:
                                f.write(chunk)
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
            logger.warning(
                "Video download attempt %d/%d failed (%s); resuming from %d bytes",
                attempt, _DOWNLOAD_RETRIES, exc, os.path.getsize(dest_path) if os.path.exists(dest_path) else 0,
            )
            time.sleep(min(2 ** attempt, 30))
            continue

        size = os.path.getsize(dest_path)
        if expected is None or size >= expected:
            return size
        logger.warning(
            "Video download incomplete (%d/%d bytes) on attempt %d/%d; resuming",
            size, expected, attempt, _DOWNLOAD_RETRIES,
        )
        time.sleep(min(2 ** attempt, 30))

    final = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
    raise RuntimeError(
        f"Video download failed after {_DOWNLOAD_RETRIES} attempts "
        f"({final}/{expected if expected is not None else '?'} bytes): {last_exc}"
    ) from last_exc
