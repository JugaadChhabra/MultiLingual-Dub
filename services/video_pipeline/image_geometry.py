"""Reading pixel dimensions out of image bytes, and fitting them to what the
render provider will accept.

Deep by construction: two functions in front of a pile of format trivia — JPEG
marker scanning, PNG's IHDR, and WEBP's three incompatible chunk layouts. Nobody
calling this needs to know any of that, and nothing here needs to know what a
video job is.

Kept in the video pipeline rather than somewhere generic because the clamp
encodes one provider's accepted range, not a universal truth.
"""
from __future__ import annotations

import struct

# HeyGen rejects renders outside this range.
DIM_MIN = 128
DIM_MAX = 4095


def dimensions(content: bytes) -> tuple[int, int] | None:
    """Return (width, height) for JPEG / PNG / WEBP bytes; None if unknown.

    None means "could not tell" — a truncated file, an unsupported format, or a
    JPEG whose markers don't lead to a frame header. Callers are expected to
    carry on and let the provider pick its own dimensions.
    """
    if len(content) < 24:
        return None
    # PNG: 8-byte sig + IHDR with width/height at offsets 16, 20
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", content[16:24])
        return int(w), int(h)
    # WEBP: "RIFF....WEBP"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        chunk = content[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", content[26:30])
            return int(w) & 0x3FFF, int(h) & 0x3FFF
        if chunk == b"VP8L":
            b0, b1, b2, b3 = content[21], content[22], content[23], content[24]
            w = 1 + (((b1 & 0x3F) << 8) | b0)
            h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return w, h
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(content[24:27], "little")
            h = 1 + int.from_bytes(content[27:30], "little")
            return w, h
    # JPEG: scan SOF markers
    if content[:2] == b"\xff\xd8":
        i = 2
        n = len(content)
        while i + 9 < n:
            if content[i] != 0xFF:
                return None
            # skip fill bytes
            while i < n and content[i] == 0xFF:
                i += 1
            if i >= n:
                return None
            marker = content[i]
            i += 1
            # Standalone markers (no length): RSTn (D0-D7), SOI (D8), EOI (D9), TEM (01)
            if marker in (0x01,) or 0xD0 <= marker <= 0xD9:
                continue
            if i + 1 >= n:
                return None
            seg_len = struct.unpack(">H", content[i:i+2])[0]
            # SOF markers (excluding DHT=C4, DAC=CC, JPG=C8)
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 7 > n:
                    return None
                h, w = struct.unpack(">HH", content[i+3:i+7])
                return int(w), int(h)
            i += seg_len
    return None


def clamp_for_render(width: int, height: int) -> tuple[int, int]:
    """Scale (width, height) into [DIM_MIN, DIM_MAX], preserving aspect ratio.

    Results are rounded to even numbers, which some encoders prefer — except at
    the extremes, where the final min/max clamp can reinstate an odd bound. See
    the tests for the exact behaviour at very wide aspect ratios.
    """
    w, h = float(width), float(height)
    longest = max(w, h)
    if longest > DIM_MAX:
        scale = DIM_MAX / longest
        w *= scale
        h *= scale
    shortest = min(w, h)
    if shortest < DIM_MIN:
        scale = DIM_MIN / shortest
        w *= scale
        h *= scale
    iw = max(DIM_MIN, min(DIM_MAX, int(round(w)) // 2 * 2))
    ih = max(DIM_MIN, min(DIM_MAX, int(round(h)) // 2 * 2))
    return iw, ih
