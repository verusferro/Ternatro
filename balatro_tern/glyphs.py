"""The game's pixel font (m6x11plus) as images.

Tern draws only system-installed fonts and plugins cannot load any, so every glyph is a white PNG cut from the real
TTF at its native 16 px (the game loads it at 200 px = 12.5x: same pixels, no anti-aliasing). Sizes are in em
(1em = the font's line = ascent 12 + descent 4 = the game's text `scale`); tinted by a color-matrix filter.

Static text is one image per string (`text_node`; the font has no kerning, so a whole string is laid out as LOVE's Text
does), tinted by a filter on its container.  DynaText places its letters itself (`gl l<k>`, scene.py), since each letter
moves on its own.

    g = Glyphs(session)
    node = ui.html.div(g.text_node(text), class_="... scf s7")
    css  = f".s7{{filter:{tint('fe5f55')}}}"
"""
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from tern_sdk import ui

FONT = Path(__file__).resolve().parent.parent / "extracted" / "resources" / "fonts" / "m6x11plus.ttf"
N = 16  # native px per em
PAD = 2  # room for side bearings in each glyph image (font px)


@lru_cache(maxsize=None)
def _font():
    return ImageFont.truetype(str(FONT), N)


@lru_cache(maxsize=4096)
def advance(text):
    """Width of `text` in font px at N (1em = N)."""
    return _font().getlength(text)


@lru_cache(maxsize=None)
def png(ch):
    """(PNG bytes, image width px) of one white glyph, drawn PAD px in from the left at the line's top."""
    f = _font()
    x0, _, x1, _ = f.getbbox(ch)
    w = max(int(round(f.getlength(ch))), x1) + PAD + max(PAD, -x0)
    im = Image.new("RGBA", (w, N), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((PAD, 0), ch, font=f, fill=(255, 255, 255, 255))
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue(), w


@lru_cache(maxsize=2048)
def text_im(text, col):
    """`text` as one native-resolution RGBA image in colour `col` (rrggbb[aa], '' = white), drawn PAD px in from the left like png()."""
    r, g, b = (int(col[i:i + 2], 16) for i in (0, 2, 4)) if col else (255, 255, 255)
    a = int(col[6:8], 16) if len(col) >= 8 else 255
    m = Image.new("L", (int(advance(text)) + 2 * PAD, N), 0)
    ImageDraw.Draw(m).text((PAD, 0), text, font=_font(), fill=a)
    im = Image.new("RGBA", m.size, (r, g, b, 0))
    im.putalpha(m)
    return im


class Glyphs:
    """Blob ids of the glyphs sent on one session (each glyph is sent once)."""

    def __init__(self, session):
        self.session, self.ids = session, {}

    def blob(self, ch):
        b = self.ids.get(ch)
        if b is None:
            b = self.ids[ch] = self.session.blob(png(ch)[0], "image/png")
        return b

    def text_node(self, text):
        """One image of a whole static text (white, tinted by a filter on its container): 1em = N px tall, PAD px of room left."""
        b = self.ids.get(text)
        if b is None:  # ponytail: one blob per distinct string for the session; LRU if a long run's strings add up
            out = io.BytesIO()
            text_im(text, "").save(out, "PNG", optimize=True)
            b = self.ids[text] = self.session.blob(out.getvalue(), "image/png")
        return ui.image(b, alt="")

    def nodes(self, text):
        return [ui.html.div(ui.image(self.blob(ch), alt=""), class_=f"gl l{k}", key=f"l{k}")
                for k, ch in enumerate(text) if not ch.isspace()]


@lru_cache(maxsize=512)
def tint(col):
    """color-matrix filter turning the white glyphs into `col` (rrggbb or rrggbbaa)."""
    r, g, b = (int(col[i:i + 2], 16) / 255 for i in (0, 2, 4))
    a = int(col[6:8], 16) / 255 if len(col) >= 8 else 1.0
    return f"color-matrix(0 0 0 0 {r:.3f} 0 0 0 0 {g:.3f} 0 0 0 0 {b:.3f} 0 0 0 {a:.3f} 0)"


if __name__ == "__main__":
    # the font's grid: 16 px renders with no anti-aliasing, and advances are whole pixels
    import numpy as np
    for ch in "Most Played Hand 0123456789$+xX!?.,":
        a = np.asarray(Image.open(io.BytesIO(png(ch)[0])))[:, :, 3]
        assert ((a == 0) | (a == 255)).all(), ch
    assert advance("WW") == 2 * advance("W") == 18
    assert text_im("Hi", "").size == (int(advance("Hi")) + 2 * PAD, N)
    assert tint("ffffff4d").endswith("0 0 0 0.302 0)")
    print("ok", advance("Most Played Hand"), png("W")[1])
