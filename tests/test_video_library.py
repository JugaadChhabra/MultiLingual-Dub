"""The video library: every render on the account, what can be done with it,
and the routes that download it or file it to the NAS.

Same set-up as test_api_recovery: a FakeRenderer, a local-folder NAS, and a
spawn that records coroutines for the test to run.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api import routes as api
from api import shared
from api.routers import library as library_routes
from services.nas import NasConfig
from services.video_pipeline import pipeline
from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobsStore
from services.video_pipeline.library import (
    NOT_ON_HEYGEN,
    NOTHING_RENDERED,
    RENDER_FAILED,
    RenderCatalog,
    build_library,
    page_of,
)
from services.video_pipeline.library_service import LibraryService
from services.video_pipeline.renderer import RenderListing
from services.video_pipeline.slots import TalkingPhotoSlots
from services.video_pipeline.store import VideoJobsStore
from services.video_pipeline.types import VideoJobSpec, VideoJobState, VideoJobSummary
from tests.fakes import FakeRenderer, FakeSpeech


def _at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=timezone.utc)


def _render(vid: str, status: str = "completed", day: int = 1) -> RenderListing:
    return RenderListing(video_id=vid, status=status, created_at=_at(day))


def _spec(title: str = "My Video") -> VideoJobSpec:
    return VideoJobSpec(script="hello", video_title=title, publish_date="12-08-2026",
                        include_overlay=False, include_music=False)


def _state(job_id: str, status: str = "failed", *, video_id: str | None = "v1",
           spec: bool = True, **summary) -> VideoJobState:
    return VideoJobState(
        job_id=job_id, status=status, error="NAS write failed" if status == "failed" else None,
        summary=VideoJobSummary(heygen_video_id=video_id, **summary),
        spec=_spec() if spec else None,
    )


# --- what each entry says ----------------------------------------------------


@pytest.mark.parametrize(
    "render, job, state, reason",
    [
        (_render("v1"), _state("j", "completed", nas_path="x.mp4"), "on_nas", None),
        (_render("v1"), _state("j", "polling"), "in_progress", None),
        (_render("v1"), _state("j", "recovering"), "in_progress", None),
        (_render("v1"), _state("j"), "sendable", "NAS write failed"),
        (_render("v1"), _state("j", spec=False), "needs_details", None),
        (_render("v1", "processing"), _state("j"), "rendering", None),
        (_render("v1", "failed"), _state("j"), "not_recoverable", RENDER_FAILED),
        (_render("v1"), _state("j", render_failed=True), "not_recoverable", RENDER_FAILED),
        (None, _state("j"), "not_recoverable", NOT_ON_HEYGEN),
        (_render("v1"), None, "needs_details", None),
        (_render("v1", "failed"), None, "not_recoverable", RENDER_FAILED),
        (_render("v1", "processing"), None, "rendering", None),
    ],
)
def test_each_render_says_what_can_be_done_with_it(render, job, state, reason) -> None:
    [entry] = build_library([render] if render else [], [job] if job else [])

    assert entry.state == state
    assert entry.reason == reason


def test_a_job_that_failed_before_submitting_says_nothing_was_rendered() -> None:
    [entry] = build_library([], [_state("j", video_id=None)])

    assert entry.state == "not_recoverable"
    assert entry.reason.startswith(NOTHING_RENDERED)
    assert "NAS write failed" in entry.reason


def test_titles_come_from_our_jobs_and_newest_is_first() -> None:
    renders = [_render("v2", day=3), _render("v1", day=1), _render("v3", day=2)]
    jobs = [_state("j", "completed", video_id="v1")]

    entries = build_library(renders, jobs)

    assert [e.video_id for e in entries] == ["v2", "v3", "v1"]
    assert entries[2].title == "My Video"
    assert entries[0].title is None


def test_a_completed_job_wins_over_an_earlier_failed_one_for_the_same_render() -> None:
    jobs = [_state("old"), _state("new", "completed")]

    [entry] = build_library([_render("v1")], jobs)

    assert (entry.job_id, entry.state) == ("new", "on_nas")


def test_the_catalog_only_relists_what_is_new() -> None:
    renderer = FakeRenderer(renders=[_render("old")])
    catalog = RenderCatalog()

    asyncio.run(catalog.renders(renderer))
    renderer.renders = [_render("new")]  # what a stopped-early listing returns
    seen = asyncio.run(catalog.renders(renderer, refresh=True))

    assert sorted(r.video_id for r in seen) == ["new", "old"]


# --- routes --------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch) -> VideoJobsStore:
    store = VideoJobsStore(persist_dir=tmp_path / "jobs")
    monkeypatch.setattr(shared, "video_jobs_store", store)
    monkeypatch.setattr(shared, "VIDEO_OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(shared, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(shared, "video_library", LibraryService(
        store, jobs_dir=tmp_path / "out", cache_dir=tmp_path / "out" / "_library"))
    return store


@pytest.fixture
def batches(tmp_path, monkeypatch) -> VideoBatchJobsStore:
    batches = VideoBatchJobsStore(persist_dir=tmp_path / "batches")
    monkeypatch.setattr(shared, "video_batch_jobs_store", batches)
    return batches


@pytest.fixture
def renderer(tmp_path, monkeypatch) -> FakeRenderer:
    renderer = FakeRenderer()
    nas = NasConfig(mode="local", root_path=str(tmp_path / "nas"),
                    server="", share="", username="", password="", domain="", port=445)
    monkeypatch.setattr(shared, "build_renderer", lambda: renderer)
    monkeypatch.setattr(shared, "build_video_deps", lambda: shared.VideoDeps(
        renderer=renderer, speech=FakeSpeech(), slots=TalkingPhotoSlots(renderer),
        heygen=None, nas=nas, email=None,
    ))
    return renderer


@pytest.fixture
def launched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(library_routes, "spawn", lambda coro, *, name: calls.append(coro))
    yield calls
    for coro in calls:
        coro.close()


@pytest.fixture
def client(store, batches, renderer, launched) -> TestClient:
    return TestClient(api.app)


def _put(store: VideoJobsStore, state: VideoJobState) -> None:
    async def _go():
        await store.create(state.job_id)
        if state.spec:
            await store.set_spec(state.job_id, state.spec)
        await store.patch_summary(state.job_id, **state.summary.model_dump(exclude_defaults=True))
        if state.status == "failed":
            await store.fail(state.job_id, state.error or "failed")
        elif state.status == "completed":
            await store.complete(state.job_id)
    asyncio.run(_go())


def _run(launched: list) -> None:
    for coro in launched:
        asyncio.run(coro)


def test_the_library_lists_counts_and_filters(client, store, renderer) -> None:
    renderer.renders = [_render("v1", day=2), _render("v2", day=1), _render("v3", "failed")]
    _put(store, _state("j1", video_id="v1"))

    body = client.get("/video/heygen/library").json()

    assert [i["video_id"] for i in body["items"]] == ["v1", "v2", "v3"]
    assert body["counts"]["sendable"] == 1
    assert body["counts"]["needs_details"] == 1
    assert body["counts"]["not_recoverable"] == 1
    assert body["listing_error"] is None

    only = client.get("/video/heygen/library", params={"state": "sendable"}).json()
    assert [i["video_id"] for i in only["items"]] == ["v1"]
    assert only["total"] == 1
    found = client.get("/video/heygen/library", params={"q": "my vid"}).json()
    assert [i["job_id"] for i in found["items"]] == ["j1"]
    picked = client.get("/video/heygen/library", params={"ids": "v2,v3"}).json()
    assert [i["video_id"] for i in picked["items"]] == ["v2", "v3"]


def test_a_listing_failure_still_shows_our_jobs(client, store, renderer, monkeypatch) -> None:
    async def broken(**_):
        raise RuntimeError("HeyGen is down")
    monkeypatch.setattr(renderer, "list_renders", broken)
    _put(store, _state("j1", "completed"))

    body = client.get("/video/heygen/library").json()

    assert [i["job_id"] for i in body["items"]] == ["j1"]
    assert "HeyGen is down" in body["listing_error"]


def test_sending_a_stranded_render_files_it_and_marks_its_batch_row(
    client, store, batches, renderer, launched, tmp_path
) -> None:
    renderer.renders = [_render("v1")]
    _put(store, _state("j1"))

    async def _batch():
        await batches.create("b1", [BatchRowState(row_index=0, script="s", video_title="My Video")])
        await batches.update_row("b1", 0, job_id="j1", status="failed", error="NAS write failed")
        await batches.row_failed("b1")
        await batches.complete("b1")
    asyncio.run(_batch())

    resp = client.post("/video/heygen/library/v1/send-to-nas")

    assert resp.status_code == 202, resp.text
    assert resp.json()["job_id"] == "j1"
    _run(launched)
    assert (tmp_path / "nas" / "12-08-2026" / "My Video.mp4").exists()
    entry = client.get("/video/heygen/library").json()["items"][0]
    assert entry["state"] == "on_nas"
    batch = asyncio.run(batches.get("b1"))
    assert (batch.status, batch.done, batch.failed_count, batch.cause) == ("completed", 1, 0, None)
    assert batch.rows[0].status == "completed"
    assert batch.rows[0].nas_path == "12-08-2026/My Video.mp4"


def test_a_render_we_know_nothing_about_needs_details_to_be_filed(
    client, store, renderer, launched, tmp_path
) -> None:
    renderer.renders = [_render("v9")]

    bare = client.post("/video/heygen/library/v9/send-to-nas")
    assert bare.status_code == 422
    assert launched == []

    resp = client.post("/video/heygen/library/v9/send-to-nas", json={
        "title": "मेष राशिफल", "publish_date": "01-11-2026",
        "include_overlay": False, "include_music": False,
    })

    assert resp.status_code == 202, resp.text
    _run(launched)
    assert (tmp_path / "nas" / "01-11-2026" / "मेष राशिफल.mp4").exists()
    entry = client.get("/video/heygen/library").json()["items"][0]
    assert (entry["state"], entry["title"]) == ("on_nas", "मेष राशिफल")


def test_a_bad_date_is_refused(client, renderer, launched) -> None:
    renderer.renders = [_render("v9")]

    resp = client.post("/video/heygen/library/v9/send-to-nas",
                       json={"title": "t", "publish_date": "2026-11-01"})

    assert resp.status_code == 422
    assert launched == []


@pytest.mark.parametrize(
    "render, job, detail",
    [
        (_render("v1", "failed"), None, RENDER_FAILED),
        (_render("v1"), _state("j", "completed", nas_path="12-08-2026/My Video.mp4"),
         "Already on the NAS at 12-08-2026/My Video.mp4"),
        (_render("v1", "processing"), None, "Still rendering on HeyGen; try again when it finishes"),
        (None, _state("j"), NOT_ON_HEYGEN),
    ],
)
def test_what_cannot_be_sent_says_why(client, store, renderer, launched, render, job, detail) -> None:
    renderer.renders = [render] if render else []
    if job:
        _put(store, job)

    resp = client.post("/video/heygen/library/v1/send-to-nas")

    assert resp.status_code == 409
    assert resp.json()["detail"] == detail
    assert launched == []


def test_an_unknown_render_is_404(client, launched) -> None:
    assert client.post("/video/heygen/library/nope/send-to-nas").status_code == 404
    assert client.get("/video/heygen/library/nope/download").status_code == 404


def test_download_hands_over_the_filed_video_when_we_have_it(client, store, renderer, tmp_path) -> None:
    renderer.renders = [_render("v1")]
    _put(store, _state("j1", "completed"))
    (tmp_path / "out" / "j1").mkdir(parents=True)
    (tmp_path / "out" / "j1" / "video_carded.mp4").write_bytes(b"carded")

    resp = client.get("/video/heygen/library/v1/download")

    assert resp.status_code == 200
    assert resp.content == b"carded"
    assert "My%20Video.mp4" in resp.headers["content-disposition"] or "My Video.mp4" in resp.headers["content-disposition"]
    assert "download" not in renderer.calls


def test_download_fetches_a_raw_render_once(client, renderer) -> None:
    renderer.renders = [_render("v9")]
    renderer.video_bytes = b"raw-render"

    first = client.get("/video/heygen/library/v9/download")
    second = client.get("/video/heygen/library/v9/download")

    assert first.content == second.content == b"raw-render"
    assert renderer.calls.count("download") == 1
    assert renderer.calls.count("render_media") == 1


def test_prepare_download_fetches_then_points_at_the_file(client, renderer) -> None:
    renderer.renders = [_render("v9")]

    prep = client.post("/video/heygen/library/v9/prepare-download")

    assert prep.status_code == 200
    assert prep.json() == {"url": "/video/heygen/library/v9/download"}
    assert client.get(prep.json()["url"]).content == renderer.video_bytes
    assert renderer.calls.count("download") == 1


def test_download_refuses_what_has_no_finished_video(client, renderer) -> None:
    renderer.renders = [_render("v1", "processing"), _render("v2", "failed")]

    assert client.get("/video/heygen/library/v1/download").status_code == 409
    failed = client.get("/video/heygen/library/v2/download")
    assert failed.status_code == 409
    assert failed.json()["detail"] == RENDER_FAILED
    assert "render_media" not in renderer.calls
    assert "download" not in renderer.calls


def test_the_library_page_is_served(client) -> None:
    assert client.get("/library").status_code == 200


# --- recover-all runs a few at a time ---------------------------------------


def test_recoveries_run_at_most_a_few_at_once(store, renderer, tmp_path, monkeypatch) -> None:
    for i in range(5):
        _put(store, _state(f"j{i}", video_id=f"v{i}"))
    nas = NasConfig(mode="local", root_path=str(tmp_path / "nas"),
                    server="", share="", username="", password="", domain="", port=445)
    running = peak = 0
    real = renderer.download

    async def slow_download(**kw):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return await real(**kw)
    renderer.download = slow_download

    async def _go():
        # A fresh semaphore, bound to this test's loop.
        monkeypatch.setattr(pipeline, "_recovery_slots", asyncio.Semaphore(pipeline.RECOVERY_CONCURRENCY))
        tasks: list = []
        await pipeline.start_recovery_of_failed(
            jobs_store=store, output_dir=tmp_path / "out", renderer=renderer, nas_config=nas,
            launch=lambda coro, *, name: tasks.append(asyncio.ensure_future(coro)),
        )
        await asyncio.gather(*tasks)

    asyncio.run(_go())

    assert peak == pipeline.RECOVERY_CONCURRENCY
    assert all(s.status == "completed" for s in asyncio.run(store.snapshot()))


# --- thumbnails ----------------------------------------------------------------


def test_a_thumbnail_is_fetched_from_heygen_once_and_kept_by_the_browser(client, renderer) -> None:
    renderer.renders = [_render("v1")]

    first = client.get("/video/heygen/library/v1/thumbnail")
    second = client.get("/video/heygen/library/v1/thumbnail")

    assert first.status_code == second.status_code == 200
    assert first.content == renderer.thumbnail_bytes
    assert first.headers["content-type"] == "image/webp"
    assert "immutable" in first.headers["cache-control"]
    assert renderer.calls.count("render_media") == 1
    assert renderer.calls.count("download") == 1


def test_a_render_not_finished_has_no_thumbnail_and_costs_no_lookup(client, renderer) -> None:
    renderer.renders = [_render("v1", "processing"), _render("v2", "failed")]

    for vid in ("v1", "v2"):
        resp = client.get(f"/video/heygen/library/{vid}/thumbnail")
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"
    assert "render_media" not in renderer.calls


def test_concurrent_requests_for_one_thumbnail_fetch_it_once(store, renderer, tmp_path) -> None:
    renderer.renders = [_render("v1")]
    lib = LibraryService(store, jobs_dir=tmp_path, cache_dir=tmp_path / "lib")
    real = renderer.download

    async def slow(**kw):
        await asyncio.sleep(0.01)
        return await real(**kw)
    renderer.download = slow

    async def _go():
        await lib.catalog.renders(renderer)
        return await asyncio.gather(*(lib.thumbnail(renderer, "v1") for _ in range(10)))

    paths = asyncio.run(_go())

    assert len(set(paths)) == 1
    assert renderer.calls.count("render_media") == 1


# --- the library is rebuilt only when something changed ------------------------


def test_polling_an_unchanged_library_does_not_copy_every_job(store, renderer, tmp_path, monkeypatch) -> None:
    renderer.renders = [_render("v1")]
    _put(store, _state("j1"))
    lib = LibraryService(store, jobs_dir=tmp_path, cache_dir=tmp_path / "lib")
    snapshots = 0
    real = store.snapshot

    async def counting():
        nonlocal snapshots
        snapshots += 1
        return await real()
    monkeypatch.setattr(store, "snapshot", counting)

    async def _go():
        for _ in range(5):
            await lib.library(renderer)
        await store.set_status("j1", "failed", "changed")
        return await lib.library(renderer)

    latest = asyncio.run(_go())

    assert snapshots == 2, "once at first, once after the job changed"
    assert latest.entries[0].job_id == "j1"


def test_a_stale_listing_is_served_at_once_and_refreshed_behind(renderer) -> None:
    renderer.renders = [_render("old")]
    catalog = RenderCatalog(max_age_seconds=0)

    async def _go():
        await catalog.renders(renderer)
        renderer.renders = [_render("new")]
        served = await catalog.renders(renderer)   # stale: returns at once
        await asyncio.sleep(0)                     # let the refresh run
        await asyncio.sleep(0)
        return [r.video_id for r in served], sorted(r.video_id for r in await catalog.renders(renderer))

    served, after = asyncio.run(_go())

    assert served == ["old"]
    assert after == ["new", "old"]


def test_page_of_filters_searches_and_counts() -> None:
    entries = build_library([_render("v1", day=2), _render("v2", day=1)], [_state("j1", video_id="v1")])

    page = page_of(entries, states={"needs_details"}, limit=10)

    assert [e.video_id for e in page.items] == ["v2"]
    assert page.total == 1
    assert page.counts["sendable"] == 1 and page.counts["needs_details"] == 1
