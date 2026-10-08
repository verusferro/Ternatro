"""Scene: every live UIBox of the game (HUD, blind select, shop, cash out, overlays, tooltips ...) as Tern nodes.

scene.lua walks the game's own UIElements (VT in game units, resolved colours, emboss/shadow/outline, text, DynaText,
sprites) and returns two parallel strings.  Structure (S) changes rarely -> the node tree is rebuilt; the per-frame
geometry/colours (F) become one stylesheet per UIBox (`sc<boxid>`), re-sent only when its rules changed.

Loop contract (the integration owner calls these):
    scene = Scene(game, session, click=lambda uie_id: handler)       # once
    scene.read()                      # every loop, before building the sheets
    scene.key                         # changes iff nodes must be rebuilt
    scene.under(), scene.over()       # nodes: below the cards (u + a layers) / above them (c, o, p layers)
    scene.update(sf, units)           # every loop: diffed per-box stylesheets
    scene.tip_node(card_id, sig, units)  # hidden popup subtree of a card (shown by CSS `:hover`), or None
"""
import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from tern_sdk import ui

from . import glyphs
from .motion import Units

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / "extracted"
UNDER, OVER = ("m", "u", "a", "f"), ("c", "t", "o", "p")  # scene.lua layer_of (Game:draw order); the cards sit between
# TEXT_OFFSET of the game's English font (G.FONTS[1]) in text-scale fractions: x = 10, y = -20 love-px * FONTSCALE / TILESIZE
TX, TY = 10 * 0.1 / 20, -20 * 0.1 / 20
DEG = 57.29578
TAU = 2 * math.pi


def _f(v):
    return float(v) if v else 0.0


def _hex(c):
    return f"#{c}" if c else "transparent"


def _shade(c, k):
    """rrggbb[aa] with rgb scaled by k (alpha kept)."""
    return "".join(f"{min(255, round(int(c[i:i + 2], 16) * k)):02x}" for i in (0, 2, 4)) + c[6:8]


@lru_cache(maxsize=None)
def _sheet(path):
    return Image.open(ROOT / path).convert("RGBA")


@lru_cache(maxsize=None)
def sprite_blob(path, cw, ch, x, y, frames):
    """PNG of one atlas cell, or a looping lossless WebP of `frames` cells along row y (AnimatedSprite)."""
    im = _sheet(path)
    b = io.BytesIO()
    if frames <= 1:
        im.crop((x * cw, y * ch, (x + 1) * cw, (y + 1) * ch)).save(b, "PNG")
        return b.getvalue(), "image/png"
    cells = [im.crop((i * cw, y * ch, (i + 1) * cw, (y + 1) * ch)) for i in range(frames)]
    cells[0].save(b, "WEBP", save_all=True, append_images=cells[1:], duration=100, loop=0, lossless=True)
    return b.getvalue(), "image/webp"


class El:
    __slots__ = ("id", "kind", "bid", "text", "shadow", "vert", "obj", "f", "tip")

    def __init__(self, f):
        self.id, self.kind, self.bid, self.text, self.shadow, self.vert = int(f[1]), f[2], int(f[3]), f[4], f[5] == "1", f[6] == "1"
        self.obj = f[7].split("\x03") if len(f) > 7 and f[7] else None
        self.tip = len(f) > 8 and f[8] == "u"
        self.f = None


class Box:
    __slots__ = ("id", "layer", "els", "node", "css")

    def __init__(self, bid, layer):
        self.id, self.layer, self.els, self.node, self.css = bid, layer, [], None, None


# ---------------------------------------------------------------- hover tips as one image
# A tip is a still of the game's popup UIBox.  Instead of a node per rect / glyph (el_css path) it is painted once with PIL
# from the same S/F rows at TIP_PX pixels per game unit and shown as one image node.
# ponytail: text/vert-text rotation is not painted (no tip rotates its text), rect / DynaText / sprite rotation is.
TIP_PX = 96
SS = 4  # supersampling of rounded-rect masks


def _rgba(c):
    c = c or "ffffff"
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(c[6:8], 16) if len(c) >= 8 else 255)


@lru_cache(maxsize=512)
def _rrect(w, h, r):
    """Anti-aliased rounded-rect mask, radius r px."""
    r = min(r, w / 2, h / 2)
    m = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius=r * SS, fill=255)
    return m.resize((w, h), Image.BOX)


def _fill(mask, col):
    lay = Image.new("RGBA", mask.size, col[:3] + (0,))
    lay.putalpha(mask if col[3] == 255 else mask.point(lambda a: a * col[3] // 255))
    return lay


def _xform(lay, px, py, r, s):
    """lay rotated by r (CSS clockwise radians) and scaled by s about (px, py)."""
    c, sn = math.cos(r) / s, math.sin(r) / s
    return lay.transform(lay.size, Image.AFFINE, (c, sn, px - c * px - sn * py, -sn, c, py + sn * px - c * py),
                         Image.BICUBIC if r else Image.NEAREST)


def _paint_rect(cv, e, P, ox, oy):
    f = e.f
    x, y, w, h, vs, vr = _f(f[2]), _f(f[3]), _f(f[4]), _f(f[5]), _f(f[6]) or 1.0, _f(f[7])
    fill, emb, embcol = f[8], _f(f[9]), f[10]
    shx, shy, shcol, outw, outcol, rad = _f(f[11]), _f(f[12]), f[13], _f(f[14]), f[15], _f(f[16])
    sw, sh = w * vs, h * vs
    pw, ph = max(1, round(sw * P)), max(1, round(sh * P))
    offs = []  # box-shadow layers, bottom -> top (the CSS path lists outline, emboss, shadow)
    if shcol:
        offs.append((round(shx * P), round(shy * P), _rgba(shcol)))
    if emb and embcol:
        offs.append((0, round(emb * P), _rgba(embcol)))
    m = max([abs(v) for dx, dy, _ in offs for v in (dx, dy)] + [0]) + 1
    box = Image.new("L", (pw + 2 * m, ph + 2 * m), 0)
    box.paste(_rrect(pw, ph, rad * P), (m, m))
    lay = Image.new("RGBA", box.size)
    for dx, dy, col in offs:  # box-shadows are clipped out of the box itself
        sm = Image.new("L", box.size, 0)
        sm.paste(_rrect(pw, ph, rad * P), (m + dx, m + dy))
        lay.alpha_composite(_fill(ImageChops.subtract(sm, box), col))
    if fill:
        lay.alpha_composite(_fill(box, _rgba(fill)))
    if outcol:
        bw = max(1, round(outw * P))
        inner = Image.new("L", box.size, 0)
        if pw > 2 * bw and ph > 2 * bw:
            inner.paste(_rrect(pw - 2 * bw, ph - 2 * bw, max(0, rad * P - bw)), (m + bw, m + bw))
        lay.alpha_composite(_fill(ImageChops.subtract(box, inner), _rgba(outcol)))
    if vr:
        lay = lay.rotate(-vr * DEG, resample=Image.BICUBIC)
    cv.alpha_composite(lay, (round((x + (w - sw) / 2 - ox) * P) - m, round((y + (h - sh) / 2 - oy) * P) - m))


def _paint_text(cv, text, col, left, top, fs, P, ox, oy, pivot=None):
    """`text` at unit position (left, top), line height fs units; pivot = (x, y, r, s) group transform in units."""
    im = glyphs.text_im(text, col)
    k = fs * P / glyphs.N
    im = im.resize((max(1, round(im.width * k)), max(1, round(glyphs.N * k))), Image.NEAREST)
    pos = (round((left - ox) * P), round((top - oy) * P))
    if pivot:
        lay = Image.new("RGBA", cv.size)
        lay.alpha_composite(im, pos)
        cv.alpha_composite(_xform(lay, (pivot[0] - ox) * P, (pivot[1] - oy) * P, pivot[2], pivot[3]))
    else:
        cv.alpha_composite(im, pos)


def _paint_dyna(cv, e, P, ox, oy):
    """DynaText still, as _dyna_css lays it out (float / bump / wobble are not part of a still)."""
    o = e.obj
    ex = e.f[17].split("\x04")[-1].split("\x03") + [""] * 18
    x, y, w, h, vs, vr = _f(ex[0]), _f(ex[1]), _f(ex[2]), _f(ex[3]), _f(ex[4]) or 1.0, _f(ex[5])
    per = [tuple(map(float, p.split(","))) for p in ex[8].split(";")] if ex[8] else []
    texts, cols_all, shcol, sc = o[1].split("\5"), o[2].split("\5"), o[3], _f(o[4])
    j = int(_f(ex[17]))
    text, cols = texts[j], cols_all[j].split(",")
    N, TH = glyphs.N, 0.83
    lay = Image.new("RGBA", cv.size)
    for shadow, dx, dy in ([(True, _f(ex[9]), _f(ex[10]))] if shcol else []) + [(False, _f(ex[11]), _f(ex[12]))]:
        cum = 0.0
        for k, ch in enumerate(text[:len(per)]):
            wem, oxe, oye, r, s, p = per[k]
            if not ch.isspace():
                top, s = (0.0, p) if shadow else (oye, s)
                lx, ly = x + _f(ex[6]) + dx + (cum - glyphs.PAD / N + oxe) * sc, y + _f(ex[7]) + dy + top * sc
                pv = (lx + (glyphs.PAD / N + 0.5 * wem) * sc, ly + 0.5 * TH * sc, r, max(s, 0.001)) if r or s != 1 else None
                _paint_text(lay, ch, shcol if shadow else cols[k % len(cols)], lx, ly, sc, P, ox, oy, pv)
            cum += wem
    if vr or vs != 1:
        lay = _xform(lay, (x + w / 2 - ox) * P, (y + h / 2 - oy) * P, vr, vs)
    cv.alpha_composite(lay)


def _paint_sprite(cv, e, im, P, ox, oy):
    ex = e.f[17].split("\x04")[-1].split("\x03")
    x, y, w, h, vs, vr = _f(ex[0]), _f(ex[1]), _f(ex[2]), _f(ex[3]), _f(ex[4]) or 1.0, _f(ex[5])
    sw, sh = w * vs, h * vs
    im = im.resize((max(1, round(sw * P)), max(1, round(sh * P))), Image.NEAREST)
    pos = (round((x + (w - sw) / 2 - ox) * P), round((y + (h - sh) / 2 - oy) * P))
    if vr:
        lay = Image.new("RGBA", cv.size)
        lay.alpha_composite(im, pos)
        im, pos = _xform(lay, (x + w / 2 - ox) * P, (y + h / 2 - oy) * P, vr, 1.0), (0, 0)
    cv.alpha_composite(im, pos)

def _paint_card(cv, c, P, ox, oy):
    """One card of a tip's CardArea: its plain face (assets.card_png) at T, scaled about its centre, with the table's drop shadow."""
    import json
    from .assets import card_png
    x, y, w, h, r, s = (_f(v) for v in c[1:7])
    im = Image.open(io.BytesIO(card_png(json.loads(c[0]))[1])).convert("RGBA")
    sw, sh = w * (s or 1.0), h * (s or 1.0)
    im = im.resize((max(1, round(sw * P)), max(1, round(sh * P))), Image.NEAREST)
    pos = (round((x + (w - sw) / 2 - ox) * P), round((y + (h - sh) / 2 - oy) * P))
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    shadow.putalpha(im.getchannel("A").point(lambda a: a * 0.3))
    lay = Image.new("RGBA", cv.size)
    lay.alpha_composite(shadow, (pos[0], pos[1] + round(0.05 * P)))
    lay.alpha_composite(im, pos)
    cv.alpha_composite(_xform(lay, (x + w / 2 - ox) * P, (y + h / 2 - oy) * P, r, 1.0) if r else lay)



def raster_tip(els, P, sprite, cards=()):
    """(PNG, left, top, width, height in game units, relative to the tip's origin) of a tip's elements, or None.
    sprite(name, x, y) -> the atlas cell as an RGBA image, or None.  cards: scene.lua build_tip K rows
    (card json, x, y, w, h, r, scale): faces drawn over the tip like the CardArea draws them."""
    vis = [e for e in els if e.f[1] == "1" and e.kind != "P"]
    if not vis:
        return None
    box = []
    for e in vis:
        f = e.f[17].split("\x04")[-1].split("\x03") if e.obj and e.obj[0] in "DSF" else e.f[2:6]
        x, y, w, h = map(_f, f[:4])
        box.append((x, y, x + w, y + h))
    for c in cards:
        x, y, w, h, s = _f(c[1]), _f(c[2]), _f(c[3]), _f(c[4]), _f(c[6]) or 1.0
        box.append((x + w / 2 - w * s / 2, y + h / 2 - h * s / 2, x + w / 2 + w * s / 2, y + h / 2 + h * s / 2))
    ox, oy = min(b[0] for b in box) - 0.6, min(b[1] for b in box) - 0.6
    cv = Image.new("RGBA", (math.ceil((max(b[2] for b in box) + 0.6 - ox) * P), math.ceil((max(b[3] for b in box) + 0.6 - oy) * P)))
    for e in vis:
        o, f = e.obj, e.f
        if e.kind == "T":
            vs, scale = _f(f[6]) or 1.0, _f(f[16])
            left, top, fs = _f(f[2]) + TX * scale, _f(f[3]) + TY * scale, scale * vs
            if e.shadow:
                k = 0.97
                _paint_text(cv, e.text, "0000004d", left + (1 - k) * _f(f[4]) / 2 + _f(f[11]), top + (1 - k) * _f(f[5]) / 2 + _f(f[12]), fs * k, P, ox, oy)
            _paint_text(cv, e.text, f[8], left, top, fs, P, ox, oy)
        elif o and o[0] == "D":
            _paint_dyna(cv, e, P, ox, oy)
        elif o and o[0] == "S":
            im = sprite(o[1], int(float(o[2])), int(float(o[3])))
            if im:
                _paint_sprite(cv, e, im, P, ox, oy)
        elif not o:
            _paint_rect(cv, e, P, ox, oy)
    for c in cards:
        _paint_card(cv, c, P, ox, oy)
    bb = cv.getchannel("A").getbbox()
    if not bb:
        return None
    out = io.BytesIO()
    cv.crop(bb).save(out, "PNG")
    return out.getvalue(), ox + bb[0] / P, oy + bb[1] / P, (bb[2] - bb[0]) / P, (bb[3] - bb[1]) / P


class Scene:
    def __init__(self, game, session, click=None):
        self.game, self.session = game, session
        game.lua.execute((HERE / "scene.lua").read_text())
        self.SC = game.lua.globals().SC
        self.click = click or (lambda uid: None)
        self.on_toggle = lambda kind: None  # "music" | "sfx": the Options menu's mute buttons
        self.SC.py_toggle = lambda kind: self.on_toggle(str(kind))
        self.atlas = {}
        for line in str(self.SC.atlases()).split("\n"):
            n, path, px, py, fr = line.split("\t")
            scaling = int(path.split("textures/")[1].split("x")[0])
            self.atlas[n] = (path, int(px) * scaling, int(py) * scaling, int(float(fr)))
        self._S = self._F = None
        self.boxes = []
        self.key = 0
        self._blobs = {}
        self.gl = glyphs.Glyphs(session)
        self._sent = {}  # sheet name -> css
        self._kf = {}  # keyframes name -> @keyframes css (DynaText float / bump), all in the `sckf` sheet
        self._tips = {}  # (card id, sig) -> (node, css)
        self._uitips = {}  # uie id -> (node, css)
        self._tipimg = {}  # tip content -> (blob, class, css): see _tip_from
        self.flame_content = {}  # "c" | "m" -> callable() -> node: the flame image (Shaders)

    def set_mute(self, music_muted, sfx_muted):
        """Labels of the Options menu's two sidecar toggles."""
        m = self.SC.mute
        m.music, m.sfx = f"Music: {'Off' if music_muted else 'On'}", f"Sound FX: {'Off' if sfx_muted else 'On'}"

    def force(self, game, what):
        """Debug: 'win' | 'over' | 'options' (BALATRO_DEBUG tokens); the game builds the real screen."""
        self.SC.force(what)

    # ------------------------------------------------------------------ read
    def read(self):
        S, F = self.SC.frame()
        S, F = str(S), str(F)
        if S != self._S:
            self._S = S
            self.key += 1
            self.boxes = self._parse_structure(S)
        self._F = F
        self._apply_frame(self.boxes, F)

    @staticmethod
    def _parse_structure(S):
        boxes, cur = [], None
        for line in S.split("\n"):
            if not line:
                continue
            f = line.split("\t")
            if f[0] == "B":
                cur = Box(int(f[1]), f[2])
                boxes.append(cur)
            else:
                cur.els.append(El(f))
        return boxes

    @staticmethod
    def _apply_frame(boxes, F):
        lines = [l for l in F.split("\n") if l]
        it = iter(lines)
        for b in boxes:
            next(it)  # box header row
            for e in b.els:
                e.f = next(it).split("\t")

    # ------------------------------------------------------------------ nodes
    def _blob(self, name, x, y, frames):
        path, cw, ch, _ = self.atlas[name]
        key = (name, x, y, frames)
        b = self._blobs.get(key)
        if b is None:
            data, mime = sprite_blob(path, cw, ch, x, y, frames)
            b = self._blobs[key] = self.session.blob(data, mime)
        return b

    def _el_node(self, e, tip=False):
        """List of nodes for one element: text with a shadow also gets a black copy underneath (ui.lua draw_self)."""
        n = self._el_node1(e, tip)
        if n is None:
            return []
        sh = (e.kind == "T" and e.shadow) or (e.obj and e.obj[0] == "D" and e.obj[3])
        if not sh:
            return [n]
        if e.kind == "T":
            txt = ui.html.div(self.gl.text_node(e.text), class_=f"sce s{e.id}s scx scf", key=f"u{e.id}s")
        else:
            txt = ui.html.div(*self._dyna_glyphs(e.obj[1]), class_=f"sce s{e.id}s scx", key=f"u{e.id}s")
        return [txt, n]

    def _el_node1(self, e, tip=False):
        cls = [f"sce s{e.id}"]
        kw = {}
        extra = []
        if e.tip and not tip:
            cls.append("scht")
            t = self.tip_uie(e.id)
            if t is not None:
                extra = [t]
        if e.bid and e.bid == e.id and not tip:  # the button root takes pointer + hover for the whole button; its
            cls.append("scb")                    # texts/children stay pointer-events:none so they never split it
            kw["on_click"] = self.click(e.bid)
        key = f"u{e.id}"
        o = e.obj
        if e.kind == "P":  # particle emitter: a pool of squares placed per frame
            return ui.html.div(*[ui.html.div(class_=f"pt q{i}", key=f"q{i}") for i in range(int(o[1]))], class_=f"sce s{e.id}", key=key)
        if e.kind == "T":
            return ui.html.div(self.gl.text_node(e.text), class_=" ".join(cls + ["scx", "scf"]), key=key, **kw)
        if o and o[0] == "D":
            return ui.html.div(*self._dyna_glyphs(o[1]), class_=" ".join(cls + ["scx"]), key=key, **kw)
        if o and o[0] == "S":
            name, x, y, frames = o[1], int(float(o[2])), int(float(o[3])), int(float(o[4]))
            if name not in self.atlas:
                return None
            img = ui.image(self._blob(name, x, y, frames), w=16, h=16, alt="", class_="scimg")
            return ui.html.div(img, *extra, class_=" ".join(cls + ["scs"]), key=key, **kw)
        if o and o[0] == "F":
            mk = self.flame_content.get(o[1])
            return ui.html.div(mk() if mk else None, class_=" ".join(cls + ["scfl", f"scfl-{o[1]}"]), key=key)
        return ui.html.div(*extra, class_=" ".join(cls), key=key, **kw)

    def _dyna_glyphs(self, texts):
        """One group per string of the DynaText (x05-separated): cycling is `.g<j>` display, the node tree stays."""
        return [ui.html.div(*self.gl.nodes(t), class_=f"dg g{j}", key=f"g{j}") for j, t in enumerate(texts.split("\5"))]

    def _box_node(self, b, tip=False):
        kids = [n for e in b.els for n in self._el_node(e, tip)]
        return ui.html.div(*kids, class_=f"scbox sb{b.id} scl-{b.layer}", key=f"b{b.id}")

    def _layer(self, layers, name):
        out = []
        for b in self.boxes:
            if b.layer in layers:
                if b.node is None:
                    b.node = self._box_node(b)
                out.append(b.node)
        return ui.html.div(*out, class_=f"scroot {name}", key=name)

    def under(self):
        return self._layer(UNDER, "scunder")

    def over(self):
        return self._layer(OVER, "scover")

    # ------------------------------------------------------------------ css
    def update(self, sf, u):
        """Per-box stylesheets for the current frame (only boxes whose rules changed are re-sent).  Boxes are laid out
        at ROOM_ORIG; the game's screen shake (ROOM offset) is one transform on the layer roots, so a shaking table
        does not re-send every box."""
        shake = f".scroot{{transform:translate({(u.rx - u.ox) * u.U:.4f}em,{(u.ry - u.oy) * u.U:.4f}em)}}"
        if self._sent.get("scshake") != shake:
            self._sent["scshake"] = shake
            sf.stylesheet("scshake", shake)
        u = Units(u.U, u.ox, u.oy)
        live = {"scshake", "sckf"}
        css_of = {f"sc{b.id}": self.box_css(b, u) for b in self.boxes}  # first: it fills _kf
        live.update(css_of)
        # keyframes go out before the boxes that use them
        for name, css in [("sckf", "".join(self._kf.values()))] + list(css_of.items()):
            if self._sent.get(name) != css:
                self._sent[name] = css
                sf.stylesheet(name, css)
        for name in [n for n in self._sent if n.startswith("sc") and n not in live and n != "sctips"]:
            del self._sent[name]
            sf.stylesheet(name, None)

    def box_css(self, b, u):
        # hidden until this box's sheet is applied (and again once it is removed): a node that reaches Tern before / after its rules
        # would sit unstyled at the stage's top-left in the inherited font size
        return f".sb{b.id}{{visibility:visible}}" + "".join(self.el_css(e, u) for e in b.els)

    def el_css(self, e, u):
        f, U = e.f, u.U
        vis = f[1] == "1"
        if not vis:
            return f".s{e.id}{{display:none}}"
        x, y, w, h, vs, vr = _f(f[2]), _f(f[3]), _f(f[4]), _f(f[5]), _f(f[6]) or 1.0, _f(f[7])
        o = e.obj
        if e.kind == "P":
            return self._particles_css(e, u)
        if e.kind == "T":
            return self._text_css(e, u, x, y, w, h, vs, vr, f[8], 0.0, 0.0, _f(f[16]), _f(f[11]), _f(f[12]), "0000004d" if e.shadow else "", e.text)
        if o and o[0] == "D":
            return self._dyna_css(e, u, o)
        if o and o[0] == "S":
            return self._sprite_css(e, u, o)
        if o and o[0] == "F":
            return self._flame_css(e, u, o)
        return self._rect_css(e, u, x, y, w, h, vs, vr)

    @staticmethod
    def _particles_css(e, u):
        """Particles:draw: each particle a square `scale` wide, rotated by `facing` about its centre."""
        U, sel = u.U, f".s{e.id}"
        parts = [p.split(",") for p in e.f[17].split(";")] if e.f[17] else []
        out = []
        for i, (x, y, s, r, col) in enumerate(parts):
            x, y, s, r = float(x), float(y), float(s), float(r)
            out.append(f"{sel} .q{i}{{left:{u.x(x - s / 2):.4f}em;top:{u.y(y - s / 2):.4f}em;width:{s * U:.4f}em;height:{s * U:.4f}em;"
                       f"transform:rotate({r * DEG:.1f}deg);background:#{col}}}")
        out += [f"{sel} .q{i}{{display:none}}" for i in range(len(parts), int(e.obj[1]))]
        return "".join(out)

    def _rect_css(self, e, u, x, y, w, h, vs, vr):
        f, U = e.f, u.U
        fill, emb, embcol = f[8], _f(f[9]), f[10]
        shx, shy, shcol, outw, outcol, rad = _f(f[11]), _f(f[12]), f[13], _f(f[14]), f[15], _f(f[16])
        chosen, prog = (f[17].split("\x04") + ["", ""])[:2]
        sw, sh = w * vs, h * vs
        left, top = u.x(x + (w - sw) / 2), u.y(y + (h - sh) / 2)
        d = [f"left:{left:.4f}em", f"top:{top:.4f}em", f"width:{sw * U:.4f}em", f"height:{sh * U:.4f}em"]
        if prog:
            frac, fc, ec = prog.split("/")
            p = float(frac) * 100
            d.append(f"background:linear-gradient(90deg,{_hex(fc)} {p:.1f}%,{_hex(ec)} {p:.1f}%)")
        elif fill:
            d.append(f"background:{_hex(fill)}")
        if rad:
            d.append(f"border-radius:{rad * U:.4f}em")
        sh_l = []
        if outcol:
            sh_l.append(f"inset 0 0 0 max(1px,{outw * U:.4f}em) {_hex(outcol)}")
        if emb and embcol:
            sh_l.append(f"0 {emb * U:.4f}em 0 {_hex(embcol)}")
        if shcol:
            sh_l.append(f"{shx * U:.4f}em {shy * U:.4f}em 0 {_hex(shcol)}")
        if sh_l:
            d.append("box-shadow:" + ",".join(sh_l))
        if vr:
            d.append(f"transform:rotate({vr * DEG:.2f}deg)")
        css = f".s{e.id}{{{';'.join(d)}}}"
        if e.bid == e.id and fill and not prog:
            # ui.lua draw_self: a hovered button's rect gets G.C.UI.HOVER (#00000055) drawn over it and its emboss is
            # darken(colour, .5) instead of .3; text is untouched.  Tern never reports hover to the game: CSS does it.
            hv = [f"background:{_hex(_shade(fill, 1 - 0x55 / 255))}"]
            if sh_l and emb and embcol:
                hv.append("box-shadow:" + ",".join(s.replace(_hex(embcol), _hex(_shade(embcol, .5 / .7))) for s in sh_l))
            css += f".s{e.id}.scb:hover{{{';'.join(hv)}}}"
        return css

    def _text_css(self, e, u, x, y, w, h, vs, vr, colour, ox, oy, scale, shx, shy, shcol, text):
        """Glyph images (glyphs.py) laid out like LOVE's Text.  The element's font-size is the text scale (in room em),
        so its own lengths are written in its own em: room lengths divided by the size."""
        U = u.U
        fs = scale * vs * U
        offx, offy = TX * scale + ox, TY * scale + oy
        sel = f".s{e.id}"

        def rule(sel, left, top, size, col):
            k = 1 / max(size, 1e-4)
            d = [f"left:{u.x(left) * k:.4f}em", f"top:{u.y(top) * k:.4f}em", f"width:{(w * U + size) * k:.4f}em",
                 "height:1em", f"font-size:{size:.4f}em", "overflow:visible"]
            tf = []
            if e.vert:
                d.append("transform-origin:0 0")
                tf.append(f"translateY({h * U * k:.4f}em) rotate(-90deg)")
            else:
                if vr or vs != 1:
                    d.append(f"transform-origin:{(w / 2 - offx) * U * k:.4f}em {(h / 2 - offy) * U * k:.4f}em")
                if vr:
                    tf.append(f"rotate({vr * DEG:.2f}deg)")
            if tf:
                d.append("transform:" + " ".join(tf))
            if col:  # tint the whole text once (one filter layer), not per glyph
                d.append(f"filter:{glyphs.tint(col)}")
            return f"{sel}{{{';'.join(d)}}}"

        css = rule(sel, x + offx, y + offy, fs, colour)
        if shcol:
            # the game draws the text again in black at 0.97 scale about the element's centre, shifted by shadow_parrallax
            k = 0.97
            sx, sy = x + offx + (1 - k) * w / 2 + shx, y + offy + (1 - k) * h / 2 + shy
            css += rule(sel + "s", sx, sy, fs * k, shcol)
        return css

    def _dyna_css(self, e, u, o):
        """DynaText as text.lua draws it: every glyph at its own offset, rotation and scale (float, bump, quiver,
        pop-in, super-juice) about its centre; the shadow copy keeps the letters' x offset and rotation but not their
        bob or juice.  The whole text is scaled/rotated by the object's VT about its centre (prep_draw).
        Float / bump run in Tern as keyframes (`_bob`), so a floating text's rules don't change every frame."""
        ex = e.f[17].split("\x04")[-1].split("\x03") + [""] * 16
        x, y, w, h, vs, vr = _f(ex[0]), _f(ex[1]), _f(ex[2]), _f(ex[3]), _f(ex[4]) or 1.0, _f(ex[5])
        ox, oy = _f(ex[6]), _f(ex[7])
        per = [tuple(map(float, p.split(","))) for p in ex[8].split(";")] if ex[8] else []
        texts, cols_all, shcol, sc = o[1].split("\5"), o[2].split("\5"), o[3], _f(o[4])
        j = int(_f(ex[17]))  # the focused string (cycling DynaText): its group shows, the others stay hidden
        text, cols = texts[j], cols_all[j].split(",")
        U, N, TH = u.U, glyphs.N, 0.83  # TH: TEXT_HEIGHT_SCALE of G.FONTS[1] (game.lua:969)
        bob = self._bob(ex[13], _f(ex[14]), _f(ex[15])) if ex[13] else None
        wob = self._wob(_f(ex[16])) if ex[16] else None

        def text_rule(sel, dx, dy, shadow):
            k = 1 / max(sc * U, 1e-4)  # this element's em is the text scale: room lengths * k
            d = [f"left:{u.x(x + ox + dx) * k:.4f}em", f"top:{u.y(y + oy + dy) * k:.4f}em", f"font-size:{sc * U:.4f}em",
                 "width:0", "height:0", "overflow:visible"]
            if vr or vs != 1:
                d.append(f"transform-origin:{(w / 2 - ox - dx) * U * k:.4f}em {(h / 2 - oy - dy) * U * k:.4f}em")
                d.append(f"transform:rotate({vr * DEG:.2f}deg) scale({vs:.3f})")
            out = [f"{sel}{{{';'.join(d)}}}{sel} .dg{{display:none}}{sel} .g{j}{{display:block}}"]
            single = shadow or len(cols) == 1
            if single and (shcol if shadow else cols[0]):
                out.append(f"{sel} .g{j} .gl{{filter:{glyphs.tint(shcol if shadow else cols[0])}}}")
            cum = 0.0
            for k, ch in enumerate(text[:len(per)]):
                wem, oxe, oye, r, s, p = per[k]
                if not ch.isspace():
                    ld = [f"left:{cum - glyphs.PAD / N + oxe:.4f}em", f"width:{glyphs.png(ch)[1] / N:.4f}em"]
                    top, s = (0.0, p) if shadow else (oye, s)
                    if top:
                        ld.append(f"top:{top:.4f}em")
                    if r or s != 1 or wob:
                        ld.append(f"transform-origin:{glyphs.PAD / N + 0.5 * wem:.4f}em {0.5 * TH:.4f}em")
                        ld.append(f"transform:rotate({r * DEG:.2f}deg) scale({max(s, 0.001):.3f})")
                    if not single and cols[k % len(cols)]:
                        ld.append(f"filter:{glyphs.tint(cols[k % len(cols)])}")
                    anims = [a for a in (None if shadow else bob, wob) if a]
                    if anims:  # text.lua phases: rate*REAL + 200*i (bob), 2*REAL + i (wobble), i 1-based
                        ld.append("animation:" + ",".join(f"{a[0]} {a[1]:.4f}s linear infinite" for a in anims))
                        ld.append("animation-delay:" + ",".join(f"{-((a[2] * (k + 1)) % TAU) / TAU * a[1]:.4f}s" for a in anims))
                    out.append(f"{sel} .g{j} .l{k}{{{';'.join(ld)}}}")
                cum += wem
            return "".join(out)

        css = text_rule(f".s{e.id}", _f(ex[11]), _f(ex[12]), False)
        if shcol:
            css = text_rule(f".s{e.id}s", _f(ex[9]), _f(ex[10]), True) + css
        return css

    def _bob(self, mode, rate, amp):
        """(keyframes name, period s, phase per letter) for text.lua's float (amp*sin) or bump
        (amp*max(0,(5+rate)*sin - 3 - rate)) as a `translate` on the letter.  Float is sampled every 15 degrees, bump
        17 times across the part above 0.  Keyframes go to the shared `sckf` sheet."""
        name = f"bt-{mode}{round(rate * 1000)}-{round(amp * 1000)}"
        if name not in self._kf:
            if mode == "f":
                pts = [(i / 24, amp * math.sin(i / 24 * TAU)) for i in range(25)]
            else:
                lo = math.asin((3 + rate) / (5 + rate)) / TAU
                pts = [(0, 0.0)] + [(t, amp * max(0.0, (5 + rate) * math.sin(t * TAU) - 3 - rate))
                                    for t in (lo + (0.5 - 2 * lo) * i / 16 for i in range(17))] + [(1, 0.0)]
            steps = "".join(f"{t * 100:.2f}%{{translate:0 {v:.3f}em}}" for t, v in pts)
            self._kf[name] = f"@keyframes {name}{{{steps}}}"
        return name, TAU / rate, 200

    def _wob(self, sign):
        """(keyframes name, period s, phase per letter) for DynaText rotate's 0.02*sin(2*REAL + i) radians as `rotate`."""
        name = f"bt-w{'n' if sign < 0 else 'p'}"
        if name not in self._kf:
            steps = [f"{i / 36 * 100:.3f}%{{rotate:{sign * 0.02 * math.sin(i / 36 * TAU) * DEG:.3f}deg}}" for i in range(37)]
            self._kf[name] = f"@keyframes {name}{{{''.join(steps)}}}"
        return name, TAU / 2, 1

    def _sprite_css(self, e, u, o):
        f, U = e.f, u.U
        ex = f[17].split("\x04")[-1].split("\x03")
        x, y, w, h, vs, vr, dis = _f(ex[0]), _f(ex[1]), _f(ex[2]), _f(ex[3]), _f(ex[4]) or 1.0, _f(ex[5]), _f(ex[6])
        sw, sh = w * vs, h * vs
        d = [f"left:{u.x(x + (w - sw) / 2):.4f}em", f"top:{u.y(y + (h - sh) / 2):.4f}em", f"width:{sw * U:.4f}em", f"height:{sh * U:.4f}em"]
        if vr:
            d.append(f"transform:rotate({vr * DEG:.2f}deg)")
        if dis > 0.005:
            d.append(f"filter:dissolve({min(dis, 1):.3f},{e.id % 97})")
        return f".s{e.id}{{{';'.join(d)}}}"

    def _flame_css(self, e, u, o):
        """The flame sprite's box (scene.lua); the baked flame.fs loop for the current intensity level fills it (app.py)."""
        U = u.U
        ex = e.f[17].split("\x04")[-1].split("\x03")
        if ex[9] != "1":
            return f".s{e.id}{{display:none}}"
        x, y, w, h = _f(ex[0]), _f(ex[1]), _f(ex[2]), _f(ex[3])
        return f".s{e.id}{{left:{u.x(x):.4f}em;top:{u.y(y):.4f}em;width:{w * U:.4f}em;height:{h * U:.4f}em}}"

    # ------------------------------------------------------------------ card popups
    def _sprite_im(self, name, x, y):
        if name in self.atlas:
            path, cw, ch, _ = self.atlas[name]
            return _sheet(path).crop((x * cw, y * ch, (x + 1) * cw, (y + 1) * ch))

    def _tip_from(self, S, F, key, U=1.0, K=""):
        """Node of a tip given SC.tip / SC.tip_uie rows: one image, painted once per distinct content (cards of the same
        kind share it), placed by its `.tp<n>` rule (collected in the `sctips` sheet); None if the tip is empty."""
        if not S:
            return None, ""
        boxes = self._parse_structure(S)[0:1]
        self._apply_frame(boxes, F)
        els = boxes[0].els
        cards = [l.split("\t") for l in K.split("\n") if l]
        ck = (U, K, tuple((e.kind, e.text, e.shadow, tuple(e.obj or ()), tuple(e.f[1:])) for e in els))
        hit = self._tipimg.get(ck)
        if hit is None:
            r = raster_tip(els, TIP_PX, self._sprite_im, cards)
            if r is None:
                hit = (None, "", "")
            else:
                png, l, t, w, h = r
                cls = f"tp{len(self._tipimg)}"
                hit = (self.session.blob(png, "image/png"), cls,
                       f".sctip.{cls}{{left:{l * U:.4f}em;top:{t * U:.4f}em}}"
                       f".sctip.{cls} .sf-image,.sctip.{cls} .sf-img-frame{{width:{w * U:.4f}em;height:{h * U:.4f}em}}")
            self._tipimg[ck] = hit
        if hit[0] is None:
            return None, ""
        return ui.html.div(ui.image(hit[0], alt=""), class_=f"sctip {hit[1]}", key=key), hit[2]

    def tip_node(self, card_id, sig, u):
        """The game's own card_h_popup for a card, relative to its upright top-left; hidden until `:hover`.
        Cached per (card, sig); its rules live in the `sctips` sheet (call tips_sheet(sf) after building nodes)."""
        key = (card_id, sig)
        hit = self._tips.get(key)
        if hit is None:
            S, F, *K = self.SC.tip(card_id)
            self._tips[key] = hit = self._tip_from(str(S), str(F), f"tip{card_id}", u.U, str(K[0]) if K else "")
            # drop stale entries of the same card
            for k in [k for k in self._tips if k[0] == card_id and k != key]:
                del self._tips[k]
        return hit[0]

    def tip_uie(self, uid):
        """Hidden tooltip of a UIElement / Tag sprite (the game's own UIBox), shown by `.scht:hover > .sctip`."""
        hit = self._uitips.get(uid)
        if hit is None:
            S, F, *K = self.SC.tip_uie(uid)
            self._uitips[uid] = hit = self._tip_from(str(S), str(F), f"utip{uid}", K=str(K[0]) if K else "")
        return hit[0]

    def tips_sheet(self, sf, live_ids):
        live_u = {e.id for b in self.boxes for e in b.els if e.tip}
        for k in [k for k in self._uitips if k not in live_u]:
            del self._uitips[k]
        css = "".join(dict.fromkeys([c for k, (n, c) in self._tips.items() if k[0] in live_ids] + [c for n, c in self._uitips.values()]))
        if self._sent.get("sctips") != css:
            self._sent["sctips"] = css
            sf.stylesheet("sctips", css)
