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


def _submit(**overrides) -> str:
    kwargs = dict(
        api_key="k",
        talking_photo_id="photo-1",
        audio_asset_id="asset-1",
        motion_prompt=None,
        video_title="T",
        callback_id="job-42",
    )
    kwargs.update(overrides)
    return heygen_client.create_avatar_iv_video(**kwargs)


def test_a_lost_submit_response_is_replayed_with_the_same_idempotency_key(transport) -> None:
    """A video create is billed. When the response is lost we replay with the
    same Idempotency-Key so HeyGen hands back the original render instead of
    creating a second one — the '12 rows in, 15 renders out' bug."""
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/videos", request.url
        keys.append(request.headers["Idempotency-Key"])
        if len(keys) == 1:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(200, json={"data": {"video_id": "v-1", "status": "pending"}})

    transport(handler)

    assert _submit() == "v-1"
    assert len(keys) == 2
    assert keys[0] == keys[1], "a replay must reuse the key or it is a new render"
    assert keys[0].startswith("job-42.")


def test_separate_submits_use_separate_idempotency_keys(transport) -> None:
    """A job that deliberately re-renders must not get its old video back."""
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json={"data": {"video_id": f"v-{len(keys)}"}})

    transport(handler)

    _submit()
    _submit()
    assert keys[0] != keys[1]


def test_a_request_in_progress_conflict_waits_and_replays(transport) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(409, json={"error": {"code": "request_in_progress", "message": "busy"}})
        return httpx.Response(200, json={"data": {"video_id": "v-1"}})

    transport(handler)

    assert _submit() == "v-1"
    assert calls == 2


def test_a_submit_that_never_gets_through_eventually_raises(transport) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    transport(handler)

    with pytest.raises(httpx.TransportError):
        _submit()


# --- avatar iv payload -----------------------------------------------------


def _capture_submit(transport, **overrides) -> dict:
    import json

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/videos", request.url
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"video_id": "v-1"}})

    transport(handler)
    _submit(**overrides)
    return seen


def test_the_v3_payload_drives_the_photo_with_the_uploaded_audio(transport) -> None:
    body = _capture_submit(transport, motion_prompt="hold still and nod gently")

    assert body["type"] == "avatar"
    assert body["avatar_id"] == "photo-1"
    assert body["audio_asset_id"] == "asset-1"
    assert body["engine"] == {"type": "avatar_iv"}
    assert body["motion_prompt"] == "hold still and nod gently"
    assert body["callback_id"] == "job-42"
    assert body["title"] == "T"


@pytest.mark.parametrize(
    ("width", "height", "aspect", "resolution"),
    [
        (1920, 1080, "16:9", "1080p"),
        (1080, 1920, "9:16", "1080p"),
        (720, 720, "1:1", "720p"),
        (1080, 1350, "4:5", "1080p"),
        (1000, 1500, "auto", "720p"),  # 2:3 has no v3 ratio; match the photo instead of padding
        (None, None, "auto", None),
    ],
)
def test_dimensions_map_to_a_v3_aspect_ratio(transport, width, height, aspect, resolution) -> None:
    body = _capture_submit(transport, width=width, height=height)

    assert body["aspect_ratio"] == aspect
    assert body.get("resolution") == resolution


# --- asset upload ----------------------------------------------------------


def test_assets_upload_as_multipart_to_v3(transport) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["host"] = request.url.host
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"data": {
            "asset_id": "a-1", "url": "https://x/a.mp3", "mime_type": "audio/mpeg", "size_bytes": 3,
        }})

    transport(handler)

    result = heygen_client.upload_asset(api_key="k", content=b"mp3", content_type="audio/mpeg")

    assert result.asset_id == "a-1"
    assert (seen["host"], seen["path"]) == ("api.heygen.com", "/v3/assets")
    assert seen["type"].startswith("multipart/form-data")
    assert b'name="file"' in seen["body"] and b"mp3" in seen["body"]


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


def test_a_file_already_fully_on_disk_is_not_downloaded_again(transport, tmp_path) -> None:
    """Recovery re-downloads into the job's folder, where a job that died on
    the NAS step left the whole video. Asking for the bytes after the end gets a
    416 from the CDN, which used to be retried six times and then fail the
    recovery of exactly the jobs recovery exists for."""
    full = b"0123456789"
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range"))
        return httpx.Response(416, headers={"content-range": f"bytes */{len(full)}"})

    transport(handler)
    dest = tmp_path / "video.mp4"
    dest.write_bytes(full)

    size = heygen_client.download_video("https://cdn.invalid/v.mp4", str(dest))

    assert size == len(full)
    assert dest.read_bytes() == full
    assert calls == ["bytes=10-"]


def test_a_stale_file_the_cdn_cannot_resume_is_downloaded_from_scratch(
    transport, tmp_path
) -> None:
    """A 416 for a file whose size does not match the render's means what's on
    disk is not a prefix of it. Start over rather than fail."""
    full = b"0123456789"
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range"))
        if request.headers.get("range"):
            return httpx.Response(416, headers={"content-range": f"bytes */{len(full)}"})
        return httpx.Response(200, headers={"content-length": str(len(full))}, content=full)

    transport(handler)
    dest = tmp_path / "video.mp4"
    dest.write_bytes(b"stale-and-longer-than-the-render")

    size = heygen_client.download_video("https://cdn.invalid/v.mp4", str(dest))

    assert size == len(full)
    assert dest.read_bytes() == full
    assert calls == ["bytes=32-", None]


def test_a_download_that_never_completes_raises(transport, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport(handler)

    with pytest.raises(RuntimeError, match="Video download failed"):
        heygen_client.download_video("https://cdn.invalid/v.mp4", str(tmp_path / "v.mp4"))


# --- photo avatars ----------------------------------------------------------


def _photo_handler(*, create_responses: list[httpx.Response], deleted: list[str], look_status: list[str]):
    creates = iter(create_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v3/assets":
            return httpx.Response(200, json={"data": {"asset_id": "img-1", "url": "u"}})
        if path == "/v3/avatars" and request.method == "POST":
            return next(creates)
        if path == "/v3/avatars/looks":
            return httpx.Response(200, json={"data": [
                {"id": "look-old", "group_id": "g-old", "status": "completed"},
                {"id": "look-new", "group_id": "g-new", "status": "completed"},
            ], "has_more": False, "next_token": None})
        if path == "/v3/avatars" and request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": "g-old", "created_at": 1},
                {"id": "g-new", "created_at": 2},
            ], "has_more": False, "next_token": None})
        if path.startswith("/v3/avatars/looks/"):
            return httpx.Response(200, json={"data": {"id": "tp-new", "status": look_status.pop(0)}})
        if request.method == "DELETE" and path.startswith("/v3/avatars/"):
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"data": {"id": path.rsplit("/", 1)[-1]}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


_CREATED = httpx.Response(200, json={"data": {"avatar_item": {"id": "tp-new", "status": "processing"}}})


def test_a_new_photo_waits_until_its_look_is_trained(transport) -> None:
    """v3 trains photo avatars asynchronously and /v3/videos rejects a look that
    is still processing, so the upload must not return early."""
    statuses = ["processing", "processing", "completed"]
    transport(_photo_handler(create_responses=[_CREATED], deleted=[], look_status=statuses))

    assert heygen_client.upload_talking_photo(api_key="k", content=b"img", content_type="image/jpeg") == "tp-new"
    assert statuses == []


def test_a_photo_that_fails_training_raises(transport) -> None:
    transport(_photo_handler(create_responses=[_CREATED], deleted=[], look_status=["failed"]))

    with pytest.raises(RuntimeError, match="photo avatar failed"):
        heygen_client.upload_talking_photo(api_key="k", content=b"img", content_type="image/jpeg")


def test_hitting_the_photo_cap_rotates_the_oldest_and_retries(transport) -> None:
    deleted: list[str] = []
    cap = httpx.Response(400, json={"error": {
        "code": "avatar_limit_exceeded", "message": "Photo avatar limit reached.",
    }})
    transport(_photo_handler(create_responses=[cap, _CREATED], deleted=deleted, look_status=["completed"]))

    photo_id = heygen_client.upload_talking_photo(api_key="k", content=b"img", content_type="image/jpeg")

    assert photo_id == "tp-new"
    assert deleted == ["g-old"], "the oldest photo group is the one that gets rotated out"


def test_an_invalid_photo_is_not_mistaken_for_the_cap(transport) -> None:
    deleted: list[str] = []
    bad = httpx.Response(400, json={"error": {"code": "invalid_parameter", "message": "max size 32MB"}})
    transport(_photo_handler(create_responses=[bad], deleted=deleted, look_status=[]))

    with pytest.raises(httpx.HTTPStatusError):
        heygen_client.upload_talking_photo(api_key="k", content=b"img", content_type="image/jpeg")
    assert deleted == []


def test_clearing_photos_only_touches_photo_avatar_groups(transport) -> None:
    deleted: list[str] = []
    seen_params: list[dict] = []
    handler = _photo_handler(create_responses=[], deleted=deleted, look_status=[])

    def spy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/avatars/looks":
            seen_params.append(dict(request.url.params))
        return handler(request)

    transport(spy)

    assert heygen_client.clear_talking_photos(api_key="k") == 2
    assert deleted == ["g-old", "g-new"]
    assert seen_params[0]["avatar_type"] == "photo_avatar"
    assert seen_params[0]["ownership"] == "private"


# --- render listing ----------------------------------------------------------


def _listing_handler(pages: list[list[str]], requested: list[str | None]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/videos", request.url
        token = request.url.params.get("token")
        requested.append(token)
        i = int(token) if token else 0
        nxt = str(i + 1) if i + 1 < len(pages) else None
        return httpx.Response(200, json={
            "data": [{"id": v, "status": "completed", "created_at": 1} for v in pages[i]],
            "has_more": nxt is not None,
            "next_token": nxt,
        })
    return handler


def test_listing_follows_the_page_token_to_the_end(transport) -> None:
    requested: list[str | None] = []
    transport(_listing_handler([["a", "b"], ["c"], ["d"]], requested))

    videos = heygen_client.list_videos(api_key="k")

    assert [v["id"] for v in videos] == ["a", "b", "c", "d"]
    assert requested == [None, "1", "2"]


def test_listing_stops_at_a_page_it_already_knows(transport) -> None:
    """2,700 renders is 28 pages. A refresh only needs what is new."""
    requested: list[str | None] = []
    transport(_listing_handler([["new"], ["old1", "old2"], ["older"]], requested))

    videos = heygen_client.list_videos(api_key="k", known=frozenset({"old1", "old2", "older"}))

    assert [v["id"] for v in videos] == ["new", "old1", "old2"]
    assert requested == [None, "1"]
