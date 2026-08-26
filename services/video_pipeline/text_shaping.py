"""Render a line of Devanagari text to a tight RGBA image.

Pillow without libraqm can't do the conjunct-forming and matra-reordering that
Devanagari requires (e.g. मिथुन, कर्क, कन्या all come out wrong), so we shape
the string with HarfBuzz (uharfbuzz) and rasterise each positioned glyph with
FreeType ourselves. This keeps the whole thing dependency-light: uharfbuzz,
freetype-py and Pillow all ship binary wheels, so no system libraqm is needed
and the result is identical on macOS and in the Linux container.

Deep by construction: callers hand in a string, a font and a pixel size and get
back an image — none of the shaping/rasterising machinery leaks out.
"""
from __future__ import annotations

import functools

import freetype
import uharfbuzz as hb
from PIL import Image


@functools.lru_cache(maxsize=8)
def _font_bytes(font_path: str) -> bytes:
    with open(font_path, "rb") as fh:
        return fh.read()


def render_line(
    text: str,
    font_path: str,
    index: int,
    px: int,
    color: tuple[int, int, int, int] = (240, 192, 80, 255),
) -> Image.Image:
    """Return a tight RGBA image of `text` shaped and rasterised at `px` pixels.

    `index` selects the face inside a .ttc collection (Nirmala UI Regular is 0).
    An empty string yields a 1x1 transparent image.
    """
    data = _font_bytes(font_path)
    face = hb.Face(data, index)
    font = hb.Font(face)
    upem = face.upem
    font.scale = (upem, upem)

    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()               # script/lang/direction from text
    hb.shape(font, buf, {"kern": True, "liga": True})

    scale = px / upem                            # font units -> pixels
    ft = freetype.Face(font_path, index)
    ft.set_pixel_sizes(0, px)

    pen = 0.0
    glyphs = []
    top_max = bot_max = 0.0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        ft.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER)
        bmp = ft.glyph.bitmap
        ox = pen + pos.x_offset * scale + ft.glyph.bitmap_left
        oy = pos.y_offset * scale - ft.glyph.bitmap_top   # baseline-relative top
        glyphs.append((bytes(bmp.buffer), bmp.width, bmp.rows, ox, oy))
        top_max = max(top_max, -oy)
        bot_max = max(bot_max, bmp.rows + oy)
        pen += pos.x_advance * scale

    width = int(pen) + 4
    ascent = int(top_max) + 2
    height = int(top_max + bot_max) + 4
    canvas = Image.new("RGBA", (max(width, 1), max(height, 1)), (0, 0, 0, 0))
    r, g, b, a0 = color

    for buffer, gw, gh, ox, oy in glyphs:
        if gw == 0 or gh == 0:
            continue
        glyph = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        put = glyph.load()
        for yy in range(gh):
            row = yy * gw
            for xx in range(gw):
                cov = buffer[row + xx]
                if cov:
                    put[xx, yy] = (r, g, b, cov * a0 // 255)
        canvas.alpha_composite(glyph, (int(ox), int(ascent + oy)))

    bbox = canvas.getbbox()
    return canvas.crop(bbox) if bbox else canvas
