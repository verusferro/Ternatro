"""Lua bridge: runs the unmodified Balatro Lua (LuaJIT via lupa) headless behind love.* stubs (stubs.lua)."""
import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from lupa import luajit21

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / "extracted"
if sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "balatro-tern"
elif sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library/Application Support/balatro-tern"
else:
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "balatro-tern"


def _stat(p):
    return "directory" if os.path.isdir(p) else "file" if os.path.isfile(p) else None


@lru_cache(maxsize=None)
def _font(path, size):
    return ImageFont.truetype(path, int(size))


class Game:
    """All area indexes are 0-based positions in snapshot()["areas"][name]["cards"]."""

    def __init__(self, root: str = str(ROOT), data_dir: str = str(DATA_DIR), speed: float = 2.0):
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lua = self.lua = luajit21.LuaRuntime(unpack_returned_tuples=True)
        # LuaJIT's default JIT budget (1000 traces, 64 KiB code areas, 2 MiB in all) is far too small for the game (it
        # keeps 4000+ traces): every time it runs out, LuaJIT flushes ALL compiled code and starts over, and after a
        # while of play that happened hundreds of times a second ("failed to allocate mcode memory"), leaving the game
        # in the interpreter (~12x slower update, most of a CPU core).
        lua.execute("jit.opt.start('maxtrace=8000', 'sizemcode=256', 'maxmcode=65536')")
        g = lua.globals()
        g.ROOT = str(Path(root)) + "/"
        g.DATA = str(self.data_dir) + "/"
        g.PYLS = lambda p: lua.table_from(sorted(os.listdir(p)) if os.path.isdir(p) else [])
        g.PYSTAT = _stat
        g.PYMKDIR = lambda p: os.makedirs(p, exist_ok=True)
        g.PYFONTW = lambda f, n, t: _font(f, n).getlength(t)
        g.PYFONTH = lambda f, n: sum(_font(f, n).getmetrics())
        lua.execute((HERE / "stubs.lua").read_text())
        self.G = g.G
        self.BT = g.BT
        self.BT.boot(speed)

    # -- lifecycle --
    def new_run(self, seed: str | None = None, deck: str = "b_red", stake: int = 1) -> None:
        self.BT.new_run(seed, deck, stake)

    def has_save(self) -> bool:
        return bool(self.BT.has_save())

    def continue_run(self) -> bool:
        return bool(self.BT.continue_run())

    def save_path(self) -> str:
        return str(self.data_dir / "1" / "save.jkr")

    def tick(self, dt: float = 1 / 60) -> None:
        self.BT.tick(dt)

    def busy(self) -> bool:
        return bool(self.BT.busy())

    # -- queries --
    def snapshot(self) -> dict:
        return json.loads(self.BT.snapshot())

    def describe(self, card_id: int) -> list[str]:
        return list(self.BT.describe(card_id).values())

    def drain_feed(self) -> list[dict]:
        out = json.loads(self.BT.drain())
        for e in out:
            if e["kind"] == "hand":
                for k in ("name", "chips", "mult", "level"):
                    e.setdefault(k, None)
        return out

    # -- actions (True if accepted) --
    def toggle(self, area: str, idx: int) -> bool: return bool(self.BT.toggle(area, idx))
    def play(self) -> bool: return bool(self.BT.play())
    def discard(self) -> bool: return bool(self.BT.discard())
    def sort_hand(self, by: str) -> bool: return bool(self.BT.sort_hand(by))
    def move(self, area: str, i: int, j: int) -> bool: return bool(self.BT.move(area, i, j))
    def select_blind(self) -> bool: return bool(self.BT.select_blind())
    def skip_blind(self) -> bool: return bool(self.BT.skip_blind())
    def cash_out(self) -> bool: return bool(self.BT.cash_out())
    def next_round(self) -> bool: return bool(self.BT.next_round())
    def reroll(self) -> bool: return bool(self.BT.reroll())
    def buy(self, area: str, idx: int, use: bool = False) -> bool: return bool(self.BT.buy(area, idx, use))
    def sell(self, area: str, idx: int) -> bool: return bool(self.BT.sell(area, idx))
    def use(self, area: str, idx: int) -> bool: return bool(self.BT.use(area, idx))
    def skip_pack(self) -> bool: return bool(self.BT.skip_pack())
    def button(self, name: str, ref: object | None = None) -> bool: return bool(self.BT.button(name, ref))


def _pump(g: Game, cond, max_ticks=20000):
    for n in range(max_ticks):
        if cond():
            return n
        g.tick()
    raise TimeoutError(f"pump timed out; state={g.snapshot()['state']}")


def _settled(g: Game, *states):
    s = g.snapshot()
    return s["state"] in states and not s["busy"]


def _selfcheck():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="balatro-tern-")
    try:
        g = Game(data_dir=tmp)
        g.new_run("TESTSEED")
        n = _pump(g, lambda: _settled(g, "BLIND_SELECT"))
        print("blind select after", n, "ticks")
        assert g.select_blind()
        _pump(g, lambda: _settled(g, "SELECTING_HAND"))
        s = g.snapshot()
        assert len(s["areas"]["hand"]["cards"]) == 8, s["areas"]["hand"]
        print("hand:", [c["front"] for c in s["areas"]["hand"]["cards"]], "target", s["hud"]["target"])
        card = s["areas"]["hand"]["cards"][0]
        print("describe:", g.describe(card["id"]))
        assert g.sort_hand("suit") and not g.play()  # nothing highlighted -> invalid
        g.G.GAME.blind.chips = 100  # selfcheck only: make the blind beatable with naive plays
        g.drain_feed()
        tick_ms = []
        while True:
            _pump(g, lambda: g.snapshot()["state"] in ("ROUND_EVAL", "GAME_OVER") or _settled(g, "SELECTING_HAND"))
            s = g.snapshot()
            if s["state"] != "SELECTING_HAND":
                break
            for i in range(5):
                assert g.toggle("hand", i)
            g.tick()
            assert g.snapshot()["can"]["play"] and g.play()
            g.tick()
            while g.snapshot()["state"] == "HAND_PLAYED" or g.busy():
                t0 = time.perf_counter(); g.tick(); tick_ms.append((time.perf_counter() - t0) * 1000)
            h = g.snapshot()["hud"]
            print("score", h["chips"], "/", h["target"], "hands left", h["hands_left"])
        print("tick ms while scoring: mean %.2f max %.2f (%d ticks)" % (sum(tick_ms) / len(tick_ms), max(tick_ms), len(tick_ms)))
        feed = g.drain_feed()
        print("feed:", len(feed), "e.g.", [e for e in feed if e["kind"] in ("eval", "hand")][:4])
        s = g.snapshot()
        print("state", s["state"], "dollars", s["hud"]["dollars"])
        if s["state"] == "GAME_OVER":
            print("lost (play-first-5 strategy)"); return
        _pump(g, lambda: g.cash_out() or False)
        _pump(g, lambda: _settled(g, "SHOP") and len(g.snapshot()["areas"]["shop_jokers"]["cards"]) == 2)
        s = g.snapshot()
        sj = s["areas"]["shop_jokers"]["cards"]
        print("shop jokers:", [(c["name"], c["cost"]) for c in sj], "dollars", s["hud"]["dollars"], "reroll", s["shop"])
        assert len(sj) == 2
        for c in sj:
            print("  ", c["name"], "|", g.describe(c["id"]))
        bought = None
        for i, c in enumerate(sj):
            if c["cost"] <= s["hud"]["dollars"] and g.buy("shop_jokers", i):
                bought = c["key"]; break
        if bought:
            _pump(g, lambda: g.snapshot()["areas"]["jokers"]["cards"] != [] and not g.busy())
            print("bought", bought, "->", [c["key"] for c in g.snapshot()["areas"]["jokers"]["cards"]])
        g.G.GAME.dollars = 20  # selfcheck only: guarantee a booster is affordable
        s = g.snapshot()
        for i, c in enumerate(s["areas"]["shop_booster"]["cards"]):
            if g.buy("shop_booster", i):
                _pump(g, lambda: g.snapshot()["state"].endswith("_PACK") and not g.busy())
                s = g.snapshot()
                pc = s["areas"]["pack_cards"]["cards"]
                print("opened", c["name"], "->", s["state"], [x["name"] for x in pc])
                for j in range(len(pc)):
                    if g.use("pack_cards", j):
                        break
                else:  # tarot needing targets: highlight hand cards first
                    for k in range(2):
                        g.toggle("hand", k)
                    g.tick()
                    for j in range(len(pc)):
                        if g.use("pack_cards", j):
                            break
                _pump(g, lambda: _settled(g, "SHOP"))
                break
        s = g.snapshot()
        print("after pack:", s["state"], "consumeables", [c["name"] for c in s["areas"]["consumeables"]["cards"]],
              "jokers", [c["name"] for c in s["areas"]["jokers"]["cards"]], "$", s["hud"]["dollars"])
        assert g.next_round()
        _pump(g, lambda: _settled(g, "BLIND_SELECT"))
        s = g.snapshot()
        print("blind choices:", [(b["type"], b["name"], b["chips"], b["state"], b["tag"] and b["tag"]["name"]) for b in s["blind_choices"]])
        ante, dollars, jokers = s["hud"]["ante"], s["hud"]["dollars"], [c["key"] for c in s["areas"]["jokers"]["cards"]]
        json.dumps(s)
        t0 = time.perf_counter()
        for _ in range(100):
            g.snapshot()
        print("snapshot ms:", (time.perf_counter() - t0) * 10)
        t0 = time.perf_counter()
        for _ in range(300):
            g.tick()
        print("tick ms (idle blind select):", (time.perf_counter() - t0) / 300 * 1000)
        g2 = Game(data_dir=tmp)
        assert g2.has_save(), "no save at " + g2.save_path()
        assert g2.continue_run()
        _pump(g2, lambda: _settled(g2, "BLIND_SELECT"))
        s2 = g2.snapshot()
        assert (s2["hud"]["ante"], s2["hud"]["dollars"], [c["key"] for c in s2["areas"]["jokers"]["cards"]]) == (ante, dollars, jokers), (s2["hud"], jokers)
        print("continue OK: ante", ante, "$", dollars, "jokers", jokers)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _selfcheck()
