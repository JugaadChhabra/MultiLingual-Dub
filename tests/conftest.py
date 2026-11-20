"""Shared test fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import services.video_pipeline.pipeline as pipeline


@pytest.fixture(autouse=True)
def _stub_burn_cards(monkeypatch):
    """Keep ffmpeg out of the orchestration tests.

    The overlay/BGM step shells out to ffmpeg on a real render; the fakes feed
    non-mp4 bytes, so here it just copies the render through unchanged. That
    keeps "the uploaded file is the finalized render" assertions meaningful
    without a video encoder. The real burn_cards is covered by test_overlay.py.
    """
    def fake_burn_cards(src, dest, sign, publish_date, *, overlay=True, music=True):
        shutil.copyfile(src, dest)
        return Path(dest)

    monkeypatch.setattr(pipeline, "burn_cards", fake_burn_cards)


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    """Admission turns jobs away when the output volume is nearly full. Fake a
    roomy disk so no test depends on the machine it runs on; the free-disk
    tests override this."""
    from api import admission

    monkeypatch.setattr(admission, "free_disk_bytes", lambda _path: 500 * admission.GB)
