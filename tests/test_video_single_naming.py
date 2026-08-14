"""The single-render route must accept the values that decide the NAS filename.

`nas.build_video_path()` resolves to `DD-MM-YYYY/<video_title>.mp4` with no
uniqueness suffix, so a caller that cannot set the title writes every render of
the day to the same path and silently overwrites the previous one. The UI used to
send neither field.
"""
from __future__ import annotations

import inspect

from api import routes as api
from services.nas import NasConfig, NasService
from services.video_pipeline.types import VideoJobSpec


def test_single_route_accepts_title_and_publish_date() -> None:
    params = inspect.signature(api.create_heygen_video_job).parameters
    assert "video_title" in params
    assert "publish_date" in params


def test_spec_carries_publish_date_through() -> None:
    spec = VideoJobSpec(script="hi", video_title="aries_week32", publish_date="2026-08-14")
    assert spec.publish_date == "2026-08-14"
    assert spec.video_title == "aries_week32"


def test_distinct_titles_resolve_to_distinct_nas_paths() -> None:
    nas = NasService(
        NasConfig(
            mode="local", root_path="/tmp/nas-test", server="", share="",
            username="", password="", domain="", port=445,
        )
    )
    first = nas.build_video_path("2026-08-14", "aries_week32")
    second = nas.build_video_path("2026-08-14", "taurus_week32")
    assert first != second
    assert first.endswith("aries_week32.mp4")

    # and the failure this guards: one title, one path, second render wins
    same = nas.build_video_path("2026-08-14", "HeyGen Avatar IV Job")
    also_same = nas.build_video_path("2026-08-14", "HeyGen Avatar IV Job")
    assert same == also_same
