"""Game animation state -> Tern.

The game already computes every animation each frame (Moveable VT incl. juice, flame handler, attention_text
boxes, screen shake).  motion.lua packs that into one string (BT.frame); this module parses it and turns it into
a small per-frame stylesheet: transport spike result: per-`el` inline style and CSS
variables are ignored by Tern, `data-*` attrs only select static rules, but replacing a sheet (`s` verb) with
one `.m<id>{transform}` rule per node moves nodes at the full loop rate (57 sheets/s measured), so that is used.
"""
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
CARD_FIELDS = ("id", "area", "idx", "x", "y", "w", "h", "r", "s", "face", "hl", "dis", "sig")
DEG = 180 / math.pi
CARD_W, CARD_H = 2.4 * 35 / 41, 2.4 * 47 / 41  # G.CARD_W / G.CARD_H: the .mc box every card face is drawn in


class Frame:
    """Parsed BT.frame(); `key` changes whenever the node tree (not just positions) must be rebuilt."""
    __slots__ = ("t", "room", "S", "cards", "areas", "flames", "btn", "blind", "hud", "key", "snap_key", "moving", "shake")

    def __init__(self, text):
        self.room, self.S, self.cards, self.areas = (1.0, 0.7, 0.0), "", [], {}
        self.flames = self.btn = self.blind = self.hud = None
        self.t = 0.0
        self.moving = False
        self.shake = 0.0
        for line in text.split("\n"):
            f = line.split("\t")
            k = f[0]
            if k == "c":
                c = dict(zip(CARD_FIELDS, f[1:]))
                for n in ("id", "idx", "hl"):
                    c[n] = int(c[n])
                for n in ("x", "y", "w", "h", "r", "s", "dis"):
                    c[n] = float(c[n])
                self.cards.append(c)
            elif k == "T":
                # ROOM offset without its slow sine drift (that runs in CSS, drift_css): constant except during a jiggle,
                # so 1/512 em steps (~0.25 px) cost nothing at rest
                self.t, self.room = float(f[1]), (round(float(f[2]) * 512) / 512, round(float(f[3]) * 512) / 512, float(f[4]))
                self.shake = float(f[5]) if len(f) > 5 else 0.0
            elif k == "S":
                self.S = f[1]
            elif k == "a":
                self.areas[f[1]] = tuple(map(float, f[2:6]))
            elif k == "f":
                self.flames = (float(f[1]), float(f[2]), float(f[3]), float(f[4]), f[5], f[6], f[7], f[8])
            elif k == "b":
                self.btn = tuple(map(float, f[1:5]))
            elif k == "k":
                self.blind = tuple(map(float, f[1:7]))
            elif k == "h":
                self.hud = tuple(map(float, f[1:5]))
        sig = tuple((c["id"], c["area"], c["idx"], c["sig"], c["face"], c["hl"]) for c in self.cards)
        self.snap_key = (self.S, sig)
        hot = (self.flames[0] > 0.5, self.flames[2] > 0.5) if self.flames else (False, False)
        self.key = (self.snap_key, self.btn is not None, hot)


class Motion:
    def __init__(self, game):
        self.game = game
        game.lua.execute((HERE / "motion.lua").read_text())
        self._frame = game.lua.globals().BT.frame
        self.last = None
        self._pos = {}

    def read(self):
        """One cheap Lua call per loop (no snapshot).  `moving`: some card moved more than the idle sway does since the
        previous read (> 0.01 em or 0.003 rad per loop; the sway is ~0.001), so the sheet must go out at full rate; app.py caps the rest."""
        text = self._frame()
        fr = self.last = Frame(text) if text else None
        if fr:
            pos = {c["id"]: (c["x"], c["y"], c["r"], c["s"]) for c in fr.cards}
            old = self._pos
            fr.moving = any(k not in old or abs(p[0] - old[k][0]) > .01 or abs(p[1] - old[k][1]) > .01 or abs(p[2] - old[k][2]) > .003
                            or abs(p[3] - old[k][3]) > .005 for k, p in pos.items())
            self._pos = pos
        return fr

    def room_orig(self):
        g = self.game.G
        return float(g.ROOM_ORIG.x), float(g.ROOM_ORIG.y)


class Units:
    """Game units -> room CSS lengths in em (1em = one game unit, view.layout_css; U is 1).  `room` is the ROOM
    offset: the shaking one from the frame when given (cards, popups), else ROOM_ORIG (static HUD/UI layout)."""
    __slots__ = ("U", "ox", "oy", "W", "H", "rx", "ry")

    def __init__(self, U, ox, oy, room=None):
        self.U, self.ox, self.oy = U, ox, oy
        self.W, self.H = 2 * ox + 20, 2 * oy + 11.5
        self.rx, self.ry = room if room else (ox, oy)

    def x(self, v):  # game x (VT/T space) -> em from the room's left edge
        return (v + self.rx) * self.U

    def y(self, v):
        return (v + self.ry) * self.U


def units(ctx, fr=None):
    return Units(ctx.U, ctx.ox, ctx.oy, fr.room[:2] if fr else None)


def drift_css(shake):
    """update_canvas_juice's slow ROOM drift, shake*0.015*sin(0.913t) / sin(0.952t) game units, as two CSS animations on
    `.room` (25 samples a period): Tern draws it at frame rate, the sidecar never resends it.  `translate` / `transform`
    rather than left / top: layout positions snap to whole css px (a ~2 px drift then moves in visible 1 px jumps)."""
    a = shake * 0.015
    if a <= 0:
        return ".room{translate:none;transform:none}"
    out = []
    for name, w, prop in (("bt-dx", 0.913, "translate"), ("bt-dy", 0.952, "transform")):
        f = "{:.5f}em" if prop == "translate" else "translateY({:.5f}em)"
        steps = "".join(f"{i / 24 * 100:.2f}%{{{prop}:{f.format(a * math.sin(i / 24 * 2 * math.pi))}}}" for i in range(25))
        out.append(f"@keyframes {name}{{{steps}}}")
    p = (2 * math.pi / 0.913, 2 * math.pi / 0.952)
    out.append(f".room{{animation:bt-dx {p[0]:.4f}s linear infinite,bt-dy {p[1]:.4f}s linear infinite}}")
    return "".join(out)


# Extension point for other renderers:
#   @motion.nodes  fn(snap, game, ctx) -> node | None       extra children of the table's `.room` (absolute nodes)
NODE_HOOKS = []


def nodes(fn):
    NODE_HOOKS.append(fn)
    return fn


def card_rule(c, fr, U):
    """CSS rule for one card from its VT: the CARD_W x CARD_H box centred on the VT, rotated and scaled to VT.w*scale
    (smaller copies such as the deck view's 0.7 cards, squish)."""
    rx, ry, _ = fr.room
    x = (c["x"] + rx + c["w"] / 2 - CARD_W / 2) * U
    y = (c["y"] + ry + c["h"] / 2 - CARD_H / 2) * U
    sx, sy = c["s"] * c["w"] / CARD_W, c["s"] * c["h"] / CARD_H
    # left/top in 1/64 em (< 1 css px), rotation in 0.25 deg (< 0.3 px at the card's corner): idle sway then resends only when it has moved
    out = f".m{c['id']}{{left:{round(x * 64) / 64:.4f}em;top:{round(y * 64) / 64:.4f}em;transform:rotate({round(c['r'] * DEG * 4) / 4:.2f}deg) scale({sx:.3f},{sy:.3f})"
    return out + "}"


def build_sheets(fr, U):
    """{sheet name: CSS}, one sheet per card area: the swaying hand then does not resend the deck view's 52 cards."""
    out = {}
    for c in fr.cards:
        k = "motion_" + c["area"]
        out[k] = out.get(k, "") + card_rule(c, fr, U)
    return out
