"""Mouse -> the game's own G.CONTROLLER (input.lua).  No keyboard: the game stays in mouse mode, so it shows no
controller prompts, focus frames or focus popups.

A click on a node (card or UIElement) moves the virtual mouse to that node's cursor point and sends
mousemoved / mousepressed / mousereleased on consecutive ticks, so the controller resolves hover and click itself.

Loop contract (app.py): `inp.on_click(...)` handlers on nodes, `inp.pre_tick()` before every game.tick().
"""
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent

GATE_TICKS = 30  # a click waits this long for the controller lock (scoring, dealing) before it is dropped like the game drops it


class Input:
    def __init__(self, game, redraw=lambda: None):
        self.game, self.redraw = game, redraw
        game.lua.execute((HERE / "input.lua").read_text())
        self.IN = game.lua.globals().IN
        self.q = deque()  # clicks: lists of steps; a step is a list of (IN function, args)
        self.cur = None
        self.waited = 0
        self.draw_error = None  # first unexpected error out of the game's draw pass (see input.lua)

    def pre_tick(self):
        """Per game tick: the game's draw pass (collision hash), then one click step (the controller takes one event per frame)."""
        err = self.IN.draw()
        if err:
            self.draw_error = str(err)
        self._step()

    def _step(self):
        if self.cur is None:
            if not self.q:
                return
            self.cur, self.waited = list(self.q.popleft()), 0
        gate, steps = self.cur
        if gate:
            C = self.game.G.CONTROLLER
            if (C.locked and not self.game.G.SETTINGS.paused) or C.locks.frame:
                self.waited += 1
                if self.waited > GATE_TICKS:
                    self.cur = None
                return
            self.cur[0] = False
        step = steps.pop(0)
        for fn, *args in step:
            ok = getattr(self.IN, fn)(*args)
            if ok is False:  # aim at a vanished node: abandon the click
                steps.clear()
        if not steps:
            self.cur = None
            self.redraw()

    def click(self, kind: str, node_id: int):
        """Click on a game node: kind 'card' (Card.sort_id), 'uie' (Node.ID) or 'btn' (G.FUNCS name of a live button, 'name#card_sort_id' for a card's own sell/use button)."""
        if kind == "card":
            self.last_card = node_id
        self.q.append((True, [[("aim", kind, node_id)], [("down",)], [("up",)], [("away",)]]))

    WASD = {"a": "left", "d": "right", "w": "first", "s": "last"}

    def key(self, name: str):
        """a/d/w/s: move the card you last clicked left / right / first / last in its row.  Tern never tells the game where the
        mouse is, so the last clicked card stands in for the hovered one."""
        how = self.WASD.get(name)
        if how and getattr(self, "last_card", None) is not None:
            self.on_move(self.last_card, how)(None)

    def on_click(self, kind: str, node_id: int):
        """Handler for a Tern node's on_click."""
        return lambda ev: self.click(kind, node_id)

    def on_move(self, card_id: int, how: str):
        """Handler for a card's right-click menu: reorder it in its row ('left' | 'right' | 'first' | 'last'), next tick."""
        return lambda ev: self.q.append((False, [[("move", card_id, how)]]))
