from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import httpx

from services.runtime_config import RuntimeConfig, get_config_value

logger = logging.getLogger(__name__)

HEYGEN_API_BASE = "https://api.heygen.com"
HEYGEN_UPLOAD_BASE = "https://upload.heygen.com"
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
            with httpx.Client(timeout=timeout) as client:
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


def get_heygen_api_key(runtime_config: RuntimeConfig | None = None) -> str:
    api_key = get_config_value("HEYGEN_ISHWARI", runtime_config=runtime_config)
    if not api_key:
        raise ValueError("Missing HEYGEN_ISHWARI API key")
    return api_key


# Maps a character handle to the env key holding its ElevenLabs voice id.
CHARACTER_VOICE_ENV = {
    "indian": "ISHWARI_VOICE_ID",
    "us": "US_VOICE_ID",
}
DEFAULT_CHARACTER = "indian"


def get_default_voice_id(runtime_config: RuntimeConfig | None = None) -> str | None:
    return get_config_value("ISHWARI_VOICE_ID", runtime_config=runtime_config) or None


def get_voice_id_for_character(
    character: str | None, runtime_config: RuntimeConfig | None = None
) -> str | None:
    env_key = CHARACTER_VOICE_ENV.get(
        (character or DEFAULT_CHARACTER).lower(),
        CHARACTER_VOICE_ENV[DEFAULT_CHARACTER],
    )
    return get_config_value(env_key, runtime_config=runtime_config) or None


@dataclass(frozen=True)
class UploadResult:
    asset_id: str
    asset_key: str
    url: str | None


def _extract_asset(payload: dict) -> UploadResult:
    data = payload.get("data") or payload
    asset_id = data.get("id") or data.get("asset_id") or ""
    asset_key = (
        data.get("file_key")
        or data.get("image_key")
        or data.get("key")
        or asset_id
    )
    return UploadResult(
        asset_id=str(asset_id),
        asset_key=str(asset_key),
        url=data.get("url"),
    )


def upload_asset(*, api_key: str, content: bytes, content_type: str) -> UploadResult:
    headers = {"X-Api-Key": api_key, "Content-Type": content_type}
    resp = _send(
        "POST", f"{HEYGEN_UPLOAD_BASE}/v1/asset", timeout=UPLOAD_TIMEOUT,
        what="asset upload", headers=headers, content=content,
    )
    resp.raise_for_status()
    return _extract_asset(resp.json())


def _fetch_first_look_id(*, api_key: str, group_id: str) -> str | None:
    headers = {"X-Api-Key": api_key}
    resp = _send(
        "GET", f"{HEYGEN_API_BASE}/v2/avatar_group/{group_id}/avatars",
        timeout=DEFAULT_TIMEOUT, what="avatar_group avatars", headers=headers,
    )
    if resp.status_code >= 400:
        logger.warning("avatar_group/%s/avatars failed: %s %s", group_id, resp.status_code, resp.text[:200])
        return None
    data = resp.json().get("data") or {}
    looks = data.get("avatar_list") or []
    if not looks:
        return None
    first = looks[0]
    return first.get("id") if isinstance(first, dict) else None


def list_talking_photos(*, api_key: str) -> list[dict]:
    """List the user's own photo avatars (capped at 3 by HeyGen).

    /v2/avatar_group.list returns only user-owned photo avatar groups. Each
    group contains one or more "looks"; the look's id is the real
    talking_photo_id used by /v2/video/generate. We resolve the first look
    of each group.
    """
    headers = {"X-Api-Key": api_key}
    resp = _send(
        "GET", f"{HEYGEN_API_BASE}/v2/avatar_group.list", timeout=DEFAULT_TIMEOUT,
        what="avatar_group.list", headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") or {}
    groups = data.get("avatar_group_list") or []
    out: list[dict] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        gid = g.get("id")
        if not gid:
            continue
        look_id = _fetch_first_look_id(api_key=api_key, group_id=str(gid))
        if not look_id:
            continue
        out.append({
            "talking_photo_id": str(look_id),
            "group_id": str(gid),
            "name": g.get("name") or "",
            "preview_url": g.get("preview_image"),
            "created_at": g.get("created_at"),
        })
    return out


def delete_avatar_group(*, api_key: str, group_id: str) -> None:
    """Delete an entire photo-avatar group. This is what frees up a slot
    against HeyGen's 3-photo-avatar cap; deleting individual looks does not."""
    headers = {"X-Api-Key": api_key}
    resp = _send(
        "DELETE", f"{HEYGEN_API_BASE}/v2/avatar_group/{group_id}",
        timeout=DEFAULT_TIMEOUT, what="avatar_group delete", headers=headers,
    )
    if resp.status_code >= 400:
        logger.error("HeyGen avatar_group delete failed: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()


def _list_avatar_group_ids(*, api_key: str) -> list[str]:
    """Return every user-owned photo-avatar group id via /v2/avatar_group.list.

    Unlike list_talking_photos this does NOT resolve each group's look id, so it
    survives the /v2/avatar_group/{id}/avatars 'Avatar group not found' failure —
    a group_id alone is all delete_avatar_group needs to free a slot."""
    headers = {"X-Api-Key": api_key}
    resp = _send(
        "GET", f"{HEYGEN_API_BASE}/v2/avatar_group.list", timeout=DEFAULT_TIMEOUT,
        what="avatar_group.list", headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()
    groups = (body.get("data") or {}).get("avatar_group_list") or []
    return [str(g["id"]) for g in groups if isinstance(g, dict) and g.get("id")]


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


QUOTA_EXCEEDED_CODE = 401028


def _post_talking_photo(*, api_key: str, content: bytes, content_type: str) -> httpx.Response:
    headers = {"X-Api-Key": api_key, "Content-Type": content_type}
    return _send(
        "POST", f"{HEYGEN_UPLOAD_BASE}/v1/talking_photo", timeout=UPLOAD_TIMEOUT,
        what="talking_photo upload", headers=headers, content=content,
    )


def upload_talking_photo(*, api_key: str, content: bytes, content_type: str) -> str:
    """Upload a new talking photo. If the per-account cap is hit, delete the
    oldest user-owned photo avatar and retry once."""
    resp = _post_talking_photo(api_key=api_key, content=content, content_type=content_type)
    if resp.status_code == 400:
        try:
            err = resp.json()
        except Exception:
            err = {}
        if err.get("code") == QUOTA_EXCEEDED_CODE:
            logger.warning("HeyGen talking_photo cap hit; rotating oldest photo avatar")
            existing = list_talking_photos(api_key=api_key)
            if not existing:
                raise RuntimeError(f"talking_photo upload hit cap but no existing avatars to rotate: {err}")
            existing.sort(key=lambda x: x.get("created_at") or 0)
            oldest = existing[0]
            delete_avatar_group(api_key=api_key, group_id=oldest["group_id"])
            resp = _post_talking_photo(api_key=api_key, content=content, content_type=content_type)

    if resp.status_code >= 400:
        logger.error("HeyGen talking_photo upload failed: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") or body
    tp_id = data.get("talking_photo_id") or data.get("id")
    if not tp_id:
        raise RuntimeError(f"talking_photo upload returned no id: {body}")
    return str(tp_id)


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
    character: dict = {
        "type": "talking_photo",
        "talking_photo_id": talking_photo_id,
        "use_avatar_iv_model": True,
    }
    if motion_prompt:
        character["motion_prompt"] = motion_prompt

    body: dict = {
        "video_title": video_title,
        "video_inputs": [
            {
                "character": character,
                "voice": {
                    "type": "audio",
                    "audio_asset_id": audio_asset_id,
                },
            }
        ],
        "caption": False,
    }
    if width and height:
        body["dimension"] = {"width": width, "height": height}
    if callback_id:
        body["callback_id"] = callback_id

    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    # Retrying /video/generate is safe here: a RemoteProtocolError means the
    # server disconnected *without sending a response*, so the render was almost
    # certainly never accepted — no duplicate credit burn.
    resp = _send(
        "POST", f"{HEYGEN_API_BASE}/v2/video/generate", timeout=DEFAULT_TIMEOUT,
        what="video generate", headers=headers, json=body,
    )
    if resp.status_code >= 400:
        logger.error("HeyGen video generate failed: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()
    data = resp.json().get("data") or resp.json()
    video_id = data.get("video_id") or data.get("id")
    if not video_id:
        raise RuntimeError(f"HeyGen response missing video_id: {resp.text}")
    return str(video_id)


def get_video_status(*, api_key: str, video_id: str) -> dict:
    headers = {"X-Api-Key": api_key}
    resp = _send(
        "GET", f"{HEYGEN_API_BASE}/v1/video_status.get", timeout=DEFAULT_TIMEOUT,
        what="video_status.get", headers=headers, params={"video_id": video_id},
    )
    resp.raise_for_status()
    return resp.json().get("data") or resp.json()


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
            err = data.get("error") or "unknown error"
            raise RuntimeError(f"HeyGen render failed: {err}")
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
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as resp:
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
