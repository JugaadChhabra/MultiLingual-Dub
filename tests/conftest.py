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
    def fake_burn_cards(src, dest, sign, publish_date):
        shutil.copyfile(src, dest)
        return Path(dest)

    monkeypatch.setattr(pipeline, "burn_cards", fake_burn_cards)
