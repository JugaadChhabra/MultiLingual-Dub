"""Tests for image dimension parsing and render clamping.

Fixtures are hand-built headers rather than encoder output — Pillow isn't a
dependency, and these functions only ever read headers. That is a real limit
worth knowing: these prove the parser matches each format's spec, not that it
matches what any particular encoder emits in the wild.
"""
from __future__ import annotations

import struct

import pytest

from services.video_pipeline.image_geometry import (
    DIM_MAX,
    DIM_MIN,
    clamp_for_render,
    dimensions,
)


# --- fixture builders ------------------------------------------------------


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def jpeg(width: int, height: int, *, marker: int = 0xC0, preamble: bytes = b"") -> bytes:
    sof = (
        bytes([0xFF, marker])
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + preamble + sof + b"\xff\xd9" + b"\x00" * 16


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    """A non-SOF segment the scanner must skip over, e.g. APP0 or a DQT."""
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def webp_vp8(width: int, height: int) -> bytes:
    # Lossy keyframe: 3-byte frame tag, then the 3-byte sync code, then the
    # 14-bit dimensions — which puts width at offset 26 of the whole file.
    body = b"WEBP" + b"VP8 " + struct.pack("<I", 10) + b"\x00" * 3
    body += b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8l(width: int, height: int) -> bytes:
    w, h = width - 1, height - 1
    b0 = w & 0xFF
    b1 = ((w >> 8) & 0x3F) | ((h & 0x03) << 6)
    b2 = (h >> 2) & 0xFF
    b3 = (h >> 10) & 0x0F
    body = b"WEBP" + b"VP8L" + struct.pack("<I", 9) + b"\x2f" + bytes([b0, b1, b2, b3])
    return b"RIFF" + struct.pack("<I", len(body)) + body + b"\x00" * 8


def webp_vp8x(width: int, height: int) -> bytes:
    body = (
        b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00" * 4
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


# --- dimensions ------------------------------------------------------------


@pytest.mark.parametrize(
    "builder", [png, jpeg, webp_vp8, webp_vp8l, webp_vp8x], ids=lambda b: b.__name__
)
@pytest.mark.parametrize("size", [(1, 1), (640, 480), (1080, 1920), (4096, 2160)])
def test_reads_dimensions_from_every_supported_format(builder, size) -> None:
    assert dimensions(builder(*size)) == size


def test_jpeg_dimensions_survive_leading_segments() -> None:
    """A real JPEG has APP0/DQT/etc before the frame header; the scanner has to
    walk past them using each segment's length."""
    preamble = jpeg_segment(0xE0, b"JFIF\x00" + b"\x00" * 12) + jpeg_segment(0xDB, b"\x00" * 64)

    assert dimensions(jpeg(800, 600, preamble=preamble)) == (800, 600)


def test_jpeg_dimensions_are_read_from_progressive_frames_too() -> None:
    assert dimensions(jpeg(800, 600, marker=0xC2)) == (800, 600)


@pytest.mark.parametrize("marker", [0xC4, 0xC8, 0xCC], ids=["DHT", "JPG", "DAC"])
def test_lookalike_markers_are_not_mistaken_for_a_frame(marker) -> None:
    """0xC4/0xC8/0xCC sit inside the SOF numeric range but are not frames.
    Reading dimensions out of a Huffman table would be nonsense."""
    payload = struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 999, 999) + b"\x00" * 10
    content = b"\xff\xd8" + bytes([0xFF, marker]) + payload + b"\x00" * 24

    assert dimensions(content) != (999, 999)


@pytest.mark.parametrize(
    "content,reason",
    [
        (b"", "empty"),
        (b"\xff\xd8short", "under the 24-byte floor"),
        (b"\x00" * 64, "no recognisable signature"),
        (b"GIF89a" + b"\x00" * 32, "unsupported format"),
        (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"XXXX" + b"\x00" * 24, "unknown webp chunk"),
        (b"\xff\xd8" + b"\x00" * 30, "jpeg with no marker where one is required"),
    ],
)
def test_undetectable_input_returns_none(content, reason) -> None:
    assert dimensions(content) is None, reason


def test_a_truncated_jpeg_does_not_raise() -> None:
    """The commit 'handling dimension out of bound error' came from this class
    of input; a partial read must return None, never explode."""
    full = jpeg(1920, 1080)
    for cut in range(24, len(full)):
        result = dimensions(full[:cut])
        assert result is None or result == (1920, 1080)


def test_a_truncated_png_does_not_raise() -> None:
    full = png(1920, 1080)
    for cut in range(24, len(full)):
        assert dimensions(full[:cut]) == (1920, 1080)


# --- clamp_for_render ------------------------------------------------------


def test_dimensions_already_in_range_are_left_alone() -> None:
    assert clamp_for_render(1080, 1920) == (1080, 1920)


def test_odd_dimensions_are_rounded_down_to_even() -> None:
    assert clamp_for_render(1081, 1921) == (1080, 1920)


def test_oversized_images_are_scaled_down_preserving_aspect() -> None:
    w, h = clamp_for_render(8000, 6000)

    assert (w, h) == (4094, 3070)
    assert max(w, h) <= DIM_MAX
    assert abs((w / h) - (8000 / 6000)) < 0.01


def test_undersized_images_are_scaled_up() -> None:
    assert clamp_for_render(10, 10) == (DIM_MIN, DIM_MIN)
    assert clamp_for_render(64, 128) == (128, 256)


@pytest.mark.parametrize(
    "size", [(1, 1), (10, 10), (8000, 6000), (5000, 100), (100, 5000), (4096, 4096), (1, 9000)]
)
def test_every_result_is_within_the_providers_accepted_range(size) -> None:
    w, h = clamp_for_render(*size)

    assert DIM_MIN <= w <= DIM_MAX
    assert DIM_MIN <= h <= DIM_MAX


def test_extreme_aspect_ratios_hit_the_bound_and_lose_evenness() -> None:
    """Documents current behaviour, which contradicts the even-dimension intent.

    An image too wide to satisfy both bounds gets scaled up to fix the short
    side, which pushes the long side past DIM_MAX; the final min() then pins it
    to 4095 — an odd number — after the rounding-to-even has already happened.
    Aspect ratio is not preserved in this case either. Both are consequences of
    the bounds being unsatisfiable, not of the rounding.
    """
    assert clamp_for_render(5000, 100) == (DIM_MAX, DIM_MIN)
    assert clamp_for_render(100, 5000) == (DIM_MIN, DIM_MAX)
    assert DIM_MAX % 2 == 1
