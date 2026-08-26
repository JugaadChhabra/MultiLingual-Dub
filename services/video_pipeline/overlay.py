"""Burn the two branded cards onto a rendered rashifal video and mix in the BGM.

The editor used to do this by hand in a video editor: drop a static name card
(a lower-third that reads "ईश्वरी कुमारी") and a dynamic card up top that reads
"<sign> राशिफल | <day> <month>", then lay a music bed under the narration. This
reproduces that exactly, driven by the two things the pipeline already knows at
finalize time: the sign (``spec.video_title``, already Devanagari) and the
publish date.

All positions are expressed against a 1080x1920 reference frame — the numbers
the editor read off their timeline — and the composed overlay is scaled to the
actual video at burn time (see ``scale2ref`` below), so a differently-sized
render still lands correctly as long as it stays 9:16.

Deep by construction: one entry point, ``burn_cards``. The card geometry, the
Devanagari date formatting and the ffmpeg invocation all live behind it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

from services.video_pipeline.text_shaping import render_line

_ASSETS = Path(__file__).resolve().parents[2] / "assets" / "overlay"
FONT = str(_ASSETS / "nirmala.ttc")
FONT_INDEX = 0                                   # Nirmala UI Regular
CARD_BG = _ASSETS / "card_bg.png"                # dynamic pill, frame-aligned
CARD_NAME = _ASSETS / "card_name.png"            # static name card (landscape)
BGM = str(_ASSETS / "bgm.mp3")

# Reference frame the editor's numbers were read against.
REF_W, REF_H = 1080, 1920
GOLD = (240, 192, 80, 255)                       # sampled off the reference card

# Dynamic (top) card: card_bg.png sits 1:1 in the frame; nudge it up to match the
# reference, and draw the sign + date line into its pill.
BG_DY = -55
PILL = (52, 113, 726, 274)                       # non-transparent pill bbox in card_bg
CARD_FONT_PX = 50

# Static (bottom) name card: whole landscape layer scaled and centre-positioned.
NAME_SCALE = 0.94
NAME_CENTER_X = 530
NAME_CENTER_Y = 1209

# Music bed level, mixed under the narration.
BGM_VOLUME = 0.30

_DEV = str.maketrans("0123456789", "०१२३४५६७८९")
_MONTHS_HI = ["", "जनवरी", "फरवरी", "मार्च", "अप्रैल", "मई", "जून",
              "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def _format_date(publish_date: str) -> str:
    """'DD-MM-YYYY' -> 'DD महीना' in Devanagari, e.g. '26-08-2025' -> '२६ अगस्त'."""
    day, month, _year = publish_date.split("-")
    return f"{str(int(day)).translate(_DEV)} {_MONTHS_HI[int(month)]}"


def card_line(sign: str, publish_date: str) -> str:
    """The dynamic card's text, e.g. 'कन्या राशिफल | २६ अगस्त'."""
    return f"{sign} राशिफल | {_format_date(publish_date)}"


def build_overlay(sign: str, publish_date: str) -> Image.Image:
    """Compose both cards onto a transparent 1080x1920 layer."""
    canvas = Image.new("RGBA", (REF_W, REF_H), (0, 0, 0, 0))

    # Dynamic card: frame-aligned pill, nudged up, with shaped text inside it.
    bg = Image.open(CARD_BG).convert("RGBA")
    canvas.alpha_composite(bg, (0, BG_DY))
    x0, y0, x1, y1 = PILL[0], PILL[1] + BG_DY, PILL[2], PILL[3] + BG_DY
    text = render_line(card_line(sign, publish_date), FONT, FONT_INDEX, CARD_FONT_PX, GOLD)
    canvas.alpha_composite(text, ((x0 + x1) // 2 - text.width // 2,
                                  (y0 + y1) // 2 - text.height // 2))

    # Static name card: scale the whole landscape layer, centre it as a lower-third.
    name = Image.open(CARD_NAME).convert("RGBA")
    nw, nh = round(name.width * NAME_SCALE), round(name.height * NAME_SCALE)
    name = name.resize((nw, nh), Image.LANCZOS)
    canvas.alpha_composite(name, (NAME_CENTER_X - nw // 2, NAME_CENTER_Y - nh // 2))

    return canvas


def burn_cards(src: Path, dest: Path, sign: str, publish_date: str) -> Path:
    """Write `src` to `dest` with both cards burned in and the BGM mixed under.

    The overlay is composed at the reference size and scaled to the video with
    ``scale2ref`` (no probing needed). The music bed is ducked to ``BGM_VOLUME``
    and clipped to the narration length by ``amix duration=first`` + ``-shortest``.
    Assumes the render carries an audio stream (HeyGen avatar videos always do).
    """
    src, dest = Path(src), Path(dest)
    layer = dest.parent / "overlay_layer.png"
    build_overlay(sign, publish_date).save(layer)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    filtergraph = (
        "[1:v][0:v]scale2ref[ov][base];"
        "[base][ov]overlay=0:0:format=auto[v];"
        f"[2:a]volume={BGM_VOLUME}[bg];"
        "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    cmd = [
        ffmpeg, "-y",
        "-i", str(src), "-i", str(layer), "-i", BGM,
        "-filter_complex", filtergraph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg overlay failed ({proc.returncode}):\n{tail}")
    return dest
