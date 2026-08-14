"""Tests for the parts of the HeyGen client that encode past incidents.

These drive real client code through httpx.MockTransport rather than through the
VideoRenderer seam — the seam hides these paths by design, and they are exactly
where the bugs were.
"""
from __future__ import annotations

import httpx
import pytest

from services.video_pipeline import heygen_client


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Backoff is real time; the behaviour under test isn't."""
    monkeypatch.setattr(heygen_client.time, "sleep", lambda _s: None)


@pytest.fixture
def transport(monkeypatch):
    def _install(handler):
        monkeypatch.setattr(heygen_client, "_transport", httpx.MockTransport(handler))
    return _install


# --- duplicate render avoidance -------------------------------------------


def test_a_lost_submit_response_adopts_the_orphaned_render(transport) -> None:
    """/video/generate is not idempotent. When the response is lost we cannot
    tell whether HeyGen created the render, so we look it up by callback_id
    instead of resubmitting — the '12 rows in, 15 renders out' bug."""
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/video/generate":
            posts.append(request)
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        if request.url.path == "/v1/video.list":
            return httpx.Response(200, json={"data": {"videos": [{"video_id": "orphan-1"}]}})
        if request.url.path == "/v1/video_status.get":
            return httpx.Response(
                200, json={"data": {"callback_id": "job-42", "status": "processing"}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    transport(handler)

    video_id = heygen_client.create_avatar_iv_video(
        api_key="k",
        talking_photo_id="photo-1",
        audio_asset_id="asset-1",
        motion_prompt=None,
        video_title="T",
        callback_id="job-42",
    )

    assert video_id == "orphan-1"
    assert len(posts) == 1, "must not resubmit — that is what duplicates the render"


def test_a_lost_submit_with_no_orphan_resubmits(transport) -> None:
    """If the request never reached HeyGen there is nothing to adopt, and
    resubmitting is safe."""
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/video/generate":
            posts.append(request)
            if len(posts) == 1:
                raise httpx.ConnectError("never got there", request=request)
            return httpx.Response(200, json={"data": {"video_id": "fresh-1"}})
        if request.url.path == "/v1/video.list":
            return httpx.Response(200, json={"data": {"videos": []}})
        raise AssertionError(f"unexpected request: {request.url}")

    transport(handler)

    video_id = heygen_client.create_avatar_iv_video(
        api_key="k",
        talking_photo_id="photo-1",
        audio_asset_id="asset-1",
        motion_prompt=None,
        video_title="T",
        callback_id="job-42",
    )

    assert video_id == "fresh-1"
    assert len(posts) == 2


def test_without_a_correlation_id_a_lost_submit_fails_rather_than_risking_a_duplicate(
    transport,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    transport(handler)

    with pytest.raises(httpx.TransportError):
        heygen_client.create_avatar_iv_video(
            api_key="k",
            talking_photo_id="photo-1",
            audio_asset_id="asset-1",
            motion_prompt=None,
            video_title="T",
            callback_id=None,
        )


# --- resumable download ----------------------------------------------------


class _StreamThatDropsMidway(httpx.SyncByteStream):
    """A CDN that closes the connection before the body is complete."""

    def __init__(self, chunks: list[bytes], fail_after: int) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            if i == self._fail_after:
                raise httpx.RemoteProtocolError("peer closed connection")
            yield chunk


def test_a_dropped_download_resumes_from_the_bytes_already_on_disk(
    transport, tmp_path
) -> None:
    full = b"0123456789"
    ranges_requested: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges_requested.append(request.headers.get("range"))
        if len(ranges_requested) == 1:
            return httpx.Response(
                200,
                headers={"content-length": str(len(full))},
                stream=_StreamThatDropsMidway([full[:5], full[5:]], fail_after=1),
            )
        return httpx.Response(
            206,
            headers={"content-length": "5"},
            stream=httpx.ByteStream(full[5:]),
        )

    transport(handler)
    dest = tmp_path / "video.mp4"

    size = heygen_client.download_video("https://cdn.invalid/v.mp4", str(dest))

    assert size == len(full)
    assert dest.read_bytes() == full, "resume must not duplicate the prefix"
    assert ranges_requested == [None, "bytes=5-"]


def test_a_server_ignoring_our_range_does_not_duplicate_the_prefix(
    transport, tmp_path
) -> None:
    """A 200 in response to a Range request means the whole file is coming
    again, so what's on disk must be discarded rather than appended to."""
    full = b"0123456789"
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"content-length": str(len(full))},
                stream=_StreamThatDropsMidway([full[:5], full[5:]], fail_after=1),
            )
        return httpx.Response(200, headers={"content-length": str(len(full))}, content=full)

    transport(handler)
    dest = tmp_path / "video.mp4"

    size = heygen_client.download_video("https://cdn.invalid/v.mp4", str(dest))

    assert size == len(full)
    assert dest.read_bytes() == full


def test_a_download_that_never_completes_raises(transport, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport(handler)

    with pytest.raises(RuntimeError, match="Video download failed"):
        heygen_client.download_video("https://cdn.invalid/v.mp4", str(tmp_path / "v.mp4"))


# --- photo cap rotation ----------------------------------------------------


def test_hitting_the_photo_cap_rotates_the_oldest_and_retries(transport) -> None:
    uploads = 0
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploads
        path = request.url.path
        if path == "/v1/talking_photo":
            uploads += 1
            if uploads == 1:
                return httpx.Response(400, json={"code": heygen_client.QUOTA_EXCEEDED_CODE})
            return httpx.Response(200, json={"data": {"talking_photo_id": "tp-new"}})
        if path == "/v2/avatar_group.list":
            return httpx.Response(
                200,
                json={"data": {"avatar_group_list": [
                    {"id": "g-old", "created_at": 1},
                    {"id": "g-new", "created_at": 2},
                ]}},
            )
        if path.endswith("/avatars"):
            group = path.split("/")[3]
            return httpx.Response(200, json={"data": {"avatar_list": [{"id": f"look-{group}"}]}})
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport(handler)

    photo_id = heygen_client.upload_talking_photo(
        api_key="k", content=b"img", content_type="image/jpeg"
    )

    assert photo_id == "tp-new"
    assert deleted == ["g-old"], "the oldest photo group is the one that gets rotated out"
    assert uploads == 2
