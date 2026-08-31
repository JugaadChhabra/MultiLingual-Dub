"""Tests for the card + BGM overlay step.

The Devanagari date formatting is pure and always runs. The full burn — shaping,
compositing, ffmpeg mux — runs against a tiny synthetic clip when ffmpeg and the
bundled assets are present, and is skipped otherwise so the suite still passes in
a bare environment.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from services.video_pipeline import overlay
from services.video_pipeline.overlay import BGM, CARD_BG, CARD_NAME, FONT, burn_cards, card_line


def test_card_line_formats_sign_day_and_hindi_month() -> None:
    assert card_line("कन्या", "26-08-2025") == "कन्या राशिफल | २६ अगस्त"
    assert card_line("मेष", "01-01-2026") == "मेष राशिफल | १ जनवरी"
    assert card_line("मीन", "09-12-2025") == "मीन राशिफल | ९ दिसंबर"


def test_card_line_accepts_iso_date_from_html_input() -> None:
    # HTML <input type=date> yields YYYY-MM-DD; day/month must not swap with year.
    assert card_line("कन्या", "2025-08-26") == "कन्या राशिफल | २६ अगस्त"
    assert card_line("मेष", "2026-01-01") == "मेष राशिफल | १ जनवरी"


def _assets_present() -> bool:
    return all(Path(p).exists() for p in (FONT, CARD_BG, CARD_NAME, BGM))


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([exe, "-version"], capture_output=True, check=True)
        return exe
    except Exception:
        return None


@pytest.mark.skipif(not _assets_present(), reason="overlay assets not bundled")
@pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg unavailable")
def test_burn_cards_produces_video_with_audio(tmp_path: Path) -> None:
    exe = _ffmpeg()
    src = tmp_path / "src.mp4"
    # tiny 1s 9:16 clip with a tone, so the mux (video + narration + BGM) is real
    subprocess.run([
        exe, "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=108x192:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(src),
    ], capture_output=True, check=True)

    out = burn_cards(src, tmp_path / "out.mp4", "कन्या", "26-08-2025")
    assert out.exists() and out.stat().st_size > 0

    # If a system ffprobe is available, confirm the mux carried both streams.
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return
    if probe.returncode == 0 and probe.stdout.strip():
        kinds = set(probe.stdout.split())
        assert "video" in kinds and "audio" in kinds


def test_build_overlay_is_reference_sized() -> None:
    if not _assets_present():
        pytest.skip("overlay assets not bundled")
    layer = overlay.build_overlay("कन्या", "26-08-2025")
    assert layer.size == (overlay.REF_W, overlay.REF_H)
    assert layer.mode == "RGBA"
