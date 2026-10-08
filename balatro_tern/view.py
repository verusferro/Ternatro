"""Table view: the game's own layout and motion (see motion.py), plus the shared Ctx / flow `card_node`.

The table is one `.room` box of (2*ROOM_ORIG.x+20) x (2*ROOM_ORIG.y+11.5) game units.  1em in the room = 1 game unit:
the room's font-size is the fit, computed by Tern from its exact cell metrics (layout_css), so every renderer writes
game units as em (ctx.U == 1).  Every card is an absolutely positioned node `.mc.m<id>` whose transform comes from the
game's VT each frame (motion.build_sheet); HUD, screens and overlays are Scene's renderings of the game's own UIBoxes.
"""
import os

from tern_sdk import ui

from . import motion
from . import shaders
from .assets import card_png

from .motion import CARD_W, CARD_H


def boss_colour(snap, game):
    b = (snap.get("hud") or {}).get("blind") or {}
    try:
        c = game.G.P_BLINDS[b["key"]].boss_colour
        return "".join(f"{round(c[i] * 255):02x}" for i in (1, 2, 3))
    except Exception:
        return "b44430"


def bg_name(snap, game):
    """Shaders.bg_bake set name for the current state (ease_background_colour_blind)."""
    st, h = snap["state"], snap.get("hud") or {}
    pack = {"TAROT_PACK": "tarot", "SPECTRAL_PACK": "spectral", "STANDARD_PACK": "standard", "BUFFOON_PACK": "buffoon", "PLANET_PACK": "planet"}
    if st in pack:
        return pack[st], None
    if snap.get("won"):
        return "won", None
    b = h.get("blind") or {}
    if b.get("boss") and st not in ("BLIND_SELECT", "ROUND_EVAL", "SHOP"):
        return "boss", "#" + boss_colour(snap, game)
    return "small", None


class Ctx:
    """Shared state: blob cache, redraw flag, unit scale U, latest frame, baked images, mute flags."""

    def __init__(self, session):
        self.session = session
        self.dirty = True
        self.U = 1.0  # CSS length of one game unit, in em (see layout_css)
        self.Upx = 64.0  # ~device px per game unit, only to size raster bakes
        self.cols, self.rows = 120, 40
        self.ox, self.oy = 1.4, 0.66
        self.fr = None  # latest motion.Frame
        self.scene = self.input = None  # set by app
        self.faces = Faces(self)
        self.bg = None  # (key, blob id, mime) of the baked swirl, once ready
        self.flames = {}  # 'c' | 'm' -> (level, blob id)
        self.mute = (False, False)  # (music, sfx) muted; set by app each loop
        self.toggle = lambda kind: None  # app: toggle "music"/"sfx", persist
        self.resize(None, None)

    @property
    def W(self):
        return 2 * self.ox + 20

    @property
    def H(self):
        return 2 * self.oy + 11.5

    def redraw(self):
        self.dirty = True

    def blob(self, png):
        return self.session.blob(png[1], "image/png")

    def resize(self, cols, cell):
        """Tern reports the cell size rounded, so the fit itself is CSS (layout_css); here only the pane's size in cells
        and a px estimate for raster bakes."""
        self.cols = cols or 120
        try:
            self.rows = os.get_terminal_size().lines
        except OSError:
            self.rows = 40
        cw, ch = getattr(cell, "w", None) or 10, getattr(cell, "h", None) or 19
        self.Upx = max(20.0, min(self.cols * cw / self.W, self.rows * ch / self.H))
        if self.scene:
            self.scene.set_tip_px(self.Upx)
        self.dirty = True

    @property
    def fit(self):
        return self.cols, self.rows


# ---------------------------------------------------------------- stylesheet (static per U)

class Faces:
    """Card face blobs: plain atlas composite, or Shaders' baked edition/debuff loop and dissolve sequences.
    Bakes run on worker threads; until one lands the plain face is shown (ctx.dirty is raised when it arrives)."""

    def __init__(self, ctx):
        from concurrent.futures import ThreadPoolExecutor
        self.ctx, self.pool = ctx, ThreadPoolExecutor(2)
        self.done, self.pending, self.dis = {}, set(), {}

    def _bake(self, key, card, kw):
        if key in self.done or key in self.pending:
            return
        self.pending.add(key)

        def run():
            try:
                self.done[key] = shaders.snapshot_card_bake(card, **kw)
            except Exception as e:  # keep the plain face
                self.done[key] = None
                self.error = repr(e)
            self.pending.discard(key)
            self.ctx.dirty = True
        self.pool.submit(run)

    def blob(self, card, face, dis):
        """Blob id for a table card. `dis` = G dissolve value (0 when intact)."""
        ctx = self.ctx
        cid = card["id"]
        plain = lambda: ctx.blob(card_png({**card, "facing": "back" if face == "b" else "front"}))
        if face == "b":
            return plain()
        if dis > 0.005:
            d = self.dis.setdefault(cid, "out" if dis < 0.5 else "in")
            key = (cid, "dis", d, card["key"], card["edition"], card["seal"])
            kw = dict(dissolve=(0, 1) if d == "out" else (1, 0), burn=["374244", "fda200"], card_id=cid,
                      duration=0.7 if d == "out" else 0.6)
        else:
            self.dis.pop(cid, None)
            if not shaders.card_effects(card):
                return plain()
            key = (cid, "fx", card["key"], card["edition"], card["seal"], card["debuff"], card.get("front"))
            kw = dict(card_id=cid)
        self._bake(key, card, kw)
        res = self.done.get(key)
        return ctx.session.blob(res[1], res[2]) if res else plain()


def layout_css(ctx):
    """Static rules that only change with the pane's size in cells.  The stage's font-size = one game unit, the
    largest that fits the pane (--sf-cw / --sf-lh are Tern's exact cell metrics in the region's px); the stage is
    exactly the room, so the swirl, the cards and the CRT share one W x H box."""
    kw, kh = (ctx.cols - 0.5) / ctx.W, (ctx.rows - 0.3) / ctx.H
    # the pane's column stretches the stage to its full width: the room is centred in it, and like the game's screen
    # margins the extra width shows the background; the room doesn't clip, so the overlay's dim covers that too
    return (f".stage{{font-size:min(calc({kw:.5f} * var(--sf-cw)),calc({kh:.5f} * var(--sf-lh)));width:{ctx.W:.4f}em;height:{ctx.H:.4f}em}}"
            f".room{{position:relative;margin:0 auto;width:{ctx.W:.4f}em;height:{ctx.H:.4f}em}}"
            f".mc,.mt{{position:absolute;left:0;top:0;width:{CARD_W:.4f}em;height:{CARD_H:.4f}em}}"
            f".ci .sf-image{{width:{CARD_W:.4f}em;height:{CARD_H:.4f}em}}")  # the face only, not glyphs in the card's tooltip


MOVES = ("move-left", "move-right", "move-first", "move-last")  # right-click menu (Tern labels it "Move left", ...)
MOVABLE = ("jokers", "consumeables", "hand")  # rows the game lets you drag to reorder


def stage_card(card, face, dis, ctx, tip, ovl=False, movable=False):
    """Table card: .mc.m<id> (game VT left/top/transform, per-frame) > .sh (shadow) > .ci (hover, edition) > image,
    and its popup: tip = Scene's hidden card popup, in a `.mt.m<id>` sibling right after the card (same per-frame
    rule) that `.mc:hover + .mt` shows above everything: a hovered card itself is never raised (CardArea:draw keeps
    the order), so it cannot cover its neighbours' clicks.  movable: right-click menu to reorder (the game's drag).
    -> [card node, tip node or nothing]"""
    cls = ["ci"]
    menu = {m: ctx.input.on_move(card["id"], m[5:]) for m in MOVES} if movable else None
    img = ui.image(ctx.faces.blob(card, face, dis),
                   w=round(CARD_W * ctx.Upx), h=round(CARD_H * ctx.Upx), alt=card.get("name") or card["key"],
                   role="card", on_click=ctx.input.on_click("card", card["id"]), on_menu=menu)
    ov = " ovc" if ovl else ""
    node = ui.html.div(ui.html.div(ui.html.div(img, class_=" ".join(cls), key="ci"), class_="sh", key="sh"),
                       class_=f"mc m{card['id']}{ov}", key=f"c{card['id']}")
    return [node] if tip is None else [node, ui.html.div(tip, class_=f"mt m{card['id']}{ov}", key=f"t{card['id']}")]


AREA_NAMES = ("deck", "discard", "shop_jokers", "shop_vouchers", "shop_booster", "hand", "consumeables", "jokers", "play", "pack_cards", "misc")


def table(snap, game, ctx):
    """The whole screen is one `.room`: Scene's UIBoxes under the cards, the cards at their VT in the game's draw order
    (each after its own price tag / buy / use / sell boxes, Card:draw), then Scene's card-attached boxes,
    attention_text popups, overlays and tooltips above them (Game:draw order)."""
    if not snap.get("hud"):
        return ui.text("No run", key="norun")
    fr, sc = ctx.fr, ctx.scene
    where = {c["id"]: c for a in AREA_NAMES for c in snap["areas"].get(a, {}).get("cards", [])}
    u = motion.units(ctx)
    cards = []
    for fc in (fr.cards if fr else ()):
        c = where.get(fc["id"])
        if c:
            boxes = sc.card_boxes(c["id"])
            if boxes is not None:
                cards.append(boxes)
            # the area is part of the tip key: Card:align_h_popup points left in the shop, below on the top row (jokers), above elsewhere
            cards += stage_card(c, fc["face"], fc["dis"], ctx, sc.tip_node(c["id"], (fc["sig"], fc["area"]), u), fc["area"] == "misc",
                                fc["area"] in MOVABLE)
    return ui.html.div(
        sc.under(), *cards, sc.over(),
        *[n for n in (h(snap, game, ctx) for h in motion.NODE_HOOKS) if n is not None],
        class_="room ovl" if snap.get("overlay") else "room", role="table", key="room")
