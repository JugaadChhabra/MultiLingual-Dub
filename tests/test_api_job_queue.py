"""The job queue at the HTTP seam: audio jobs beyond the slot limit wait, with
a visible 1-based queue position, and start as slots free.

The fake runners block on an asyncio event the test releases, so nothing here
depends on timing. One blocking portal (one event loop) is shared by every
request, as the server has; lifespan is not run, so nothing touches the network.
"""
from __future__ import annotations

import asyncio
import time
from io import BytesIO

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient
from openpyxl import Workbook

from api import routes as api
from api import shared
from api.routers import audio as audio_routes
from api.routers import story as story_routes
from batch.job_queue import JobQueue
from batch.store import JobsStore
from tests.fakes import use_env
from tests.test_api_batch import ENV_TEXT


@pytest.fixture(autouse=True)
def _configured(monkeypatch) -> None:
    use_env(monkeypatch, ENV_TEXT)


class Gate:
    """Fake runners that start their job, then wait until released."""

    def __init__(self, portal) -> None:
        self.portal = portal
        self.events: dict[str, asyncio.Event] = {}
        self.invoked: list[str] = []
        self.fail: set[str] = set()

    def _event(self, job_id: str) -> asyncio.Event:
        return self.events.setdefault(job_id, asyncio.Event())

    async def _run(self, job_id, jobs_store):
        self.invoked.append(job_id)
        await jobs_store.start(job_id)
        await self._event(job_id).wait()
        if job_id in self.fail:
            raise RuntimeError("runner blew up")
        await jobs_store.complete(job_id, (await jobs_store.get(job_id)).summary)

    async def excel(self, *, job_id, jobs_store, **_kwargs):
        await self._run(job_id, jobs_store)

    async def story(self, *, job_id, jobs_store, **_kwargs):
        await self._run(job_id, jobs_store)

    def release(self, job_id: str) -> None:
        self.portal.call(lambda: self._event(job_id).set())


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = JobsStore()
    monkeypatch.setattr(shared, "jobs_store", store)
    monkeypatch.setattr(shared, "job_queue", JobQueue(store, limit=2))
    with start_blocking_portal() as portal:
        gate = Gate(portal)
        monkeypatch.setattr(audio_routes, "run_excel_batch_job", gate.excel)
        monkeypatch.setattr(story_routes, "run_story_job", gate.story)
        client = TestClient(api.app)
        client.portal = portal
        yield client, gate
        # Let anything still blocked finish, so the loop closes cleanly.
        for job_id in list(gate.events):
            gate.release(job_id)


def _xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["text", "emotion", "activity_name", "audio_type"])
    ws.append(["hello", "happy", "activity", "promo"])
    buff = BytesIO()
    wb.save(buff)
    return buff.getvalue()


def _submit_excel(client) -> str:
    r = client.post(
        "/batch/excel-jobs",
        files={"file": ("input.xlsx", _xlsx(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"target_languages": "hi-IN"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    return r.json()["job_id"]


def _submit_story(client) -> str:
    r = client.post("/batch/story-jobs", json={
        "title": "The stream",
        "activity_name": "thestreamstory",
        "lines": [{"text": "Hi there!", "emotion": "Cheerful"}],
        "target_languages": ["hi-IN"],
    })
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _job(client, job_id) -> dict:
    r = client.get(f"/batch/excel-jobs/{job_id}")
    assert r.status_code == 200
    return r.json()


def _states(client, ids) -> list[tuple[str, int | None]]:
    return [(j["status"], j["queue_position"]) for j in (_job(client, i) for i in ids)]


def _settle(client, ids, expected, retries: int = 2000):
    """Poll until the jobs reach the expected (status, position) pairs."""
    for _ in range(retries):
        got = _states(client, ids)
        if got == expected:
            return got
        time.sleep(0.005)
    assert _states(client, ids) == expected


def test_jobs_beyond_the_limit_wait_with_their_positions(env) -> None:
    client, _gate = env
    ids = [_submit_excel(client) for _ in range(4)]

    _settle(client, ids, [
        ("running", None), ("running", None), ("queued", 1), ("queued", 2),
    ])


def test_a_finished_job_starts_the_head_of_the_queue(env) -> None:
    client, gate = env
    ids = [_submit_excel(client) for _ in range(4)]
    _settle(client, ids, [("running", None), ("running", None), ("queued", 1), ("queued", 2)])

    gate.release(ids[0])

    _settle(client, ids, [
        ("completed", None), ("running", None), ("running", None), ("queued", 1),
    ])
    assert ids[3] not in gate.invoked


def test_a_runner_that_raises_still_frees_its_slot(env) -> None:
    client, gate = env
    ids = [_submit_excel(client) for _ in range(3)]
    _settle(client, ids, [("running", None), ("running", None), ("queued", 1)])

    gate.fail.add(ids[0])
    gate.release(ids[0])

    # The fake never records a terminal status, so the first stays "running";
    # what matters is that its slot went to the waiting job.
    _settle(client, ids[1:], [("running", None), ("running", None)])
    assert gate.invoked == ids


def test_a_story_job_waits_behind_excel_jobs_in_the_same_queue(env) -> None:
    client, gate = env
    excel = [_submit_excel(client) for _ in range(2)]
    story = _submit_story(client)

    _settle(client, excel + [story], [("running", None), ("running", None), ("queued", 1)])

    gate.release(excel[1])
    _settle(client, [story], [("running", None)])


def test_a_job_no_longer_queued_when_its_turn_comes_is_skipped(env) -> None:
    """Room for cancelling a waiting job: the queue never runs it."""
    client, gate = env
    ids = [_submit_excel(client) for _ in range(4)]
    _settle(client, ids, [("running", None), ("running", None), ("queued", 1), ("queued", 2)])

    client.portal.call(shared.jobs_store.cancel, ids[2])
    gate.release(ids[0])

    _settle(client, ids, [
        ("completed", None), ("running", None), ("cancelled", None), ("running", None),
    ])
    assert ids[2] not in gate.invoked


def test_a_job_cancelled_while_waiting_never_runs(env) -> None:
    client, gate = env
    ids = [_submit_excel(client) for _ in range(3)]
    _settle(client, ids, [("running", None), ("running", None), ("queued", 1)])

    assert client.post(f"/batch/excel-jobs/{ids[2]}/cancel").status_code == 200
    gate.release(ids[0])

    _settle(client, ids, [("completed", None), ("running", None), ("cancelled", None)])
    assert ids[2] not in gate.invoked
