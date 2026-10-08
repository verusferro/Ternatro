"""Headless greedy Balatro bot over the public Game API (Phase 4/5 exit check).

    .venv/bin/python tools/bot.py --seeds 5 [--assist] [--speed 8]

Pure greedy by default. `--assist` is a CHEAT flag: tops up dollars at every shop (G.GAME.dollars poke) and, if
still short, drops the blind target (clearly reported in the summary).
Hang = (state, key HUD values) unchanged for --stall ticks -> raises (reported per seed, exit code 1).
"""
import argparse
import itertools
import random
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from balatro_tern.game import Game  # noqa: E402

RANKS = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "Jack": 11, "Queen": 12, "King": 13, "Ace": 14}
CHIPS = {"Jack": 10, "Queen": 10, "King": 10, "Ace": 11}
# name: (chips, mult, rank-strength)
BASE = {"High Card": (5, 1, 0), "Pair": (10, 2, 1), "Two Pair": (20, 2, 2), "Three of a Kind": (30, 3, 3), "Straight": (30, 4, 4),
        "Flush": (35, 4, 5), "Full House": (40, 4, 6), "Four of a Kind": (60, 7, 7), "Straight Flush": (100, 8, 8)}
PACK_KINDS = ("arcana", "celestial", "spectral", "standard", "buffoon")


class Hang(Exception):
    pass


def evaluate(cards):
    """cards: list of snapshot cards (1..5). -> (hand name, scoring cards)."""
    rs = [RANKS.get(c["rank"], 0) for c in cards]
    cnt = Counter(rs)
    by = sorted(cnt.values(), reverse=True)
    flush = len(cards) == 5 and len({c["suit"] for c in cards}) == 1
    u = sorted(set(rs))
    straight = len(cards) == 5 and len(u) == 5 and (u[-1] - u[0] == 4 or u == [2, 3, 4, 5, 14])
    if straight and flush: return "Straight Flush", cards
    if by[0] == 4: return "Four of a Kind", [c for c, r in zip(cards, rs) if cnt[r] == 4]
    if by[:2] == [3, 2]: return "Full House", cards
    if flush: return "Flush", cards
    if straight: return "Straight", cards
    if by[0] == 3: return "Three of a Kind", [c for c, r in zip(cards, rs) if cnt[r] == 3]
    if by[:2] == [2, 2]: return "Two Pair", [c for c, r in zip(cards, rs) if cnt[r] == 2]
    if by[0] == 2: return "Pair", [c for c, r in zip(cards, rs) if cnt[r] == 2]
    return "High Card", [max(zip(rs, cards), key=lambda t: t[0])[1]]


def best_play(hand, need5=False):
    """-> (score, hand name, [indices]) over every subset of non-debuffed cards."""
    idx = [i for i, c in enumerate(hand) if not c["debuff"] and c["rank"]]
    best = (-1, None, [])
    for k in range(1, 6):
        for combo in itertools.combinations(idx, k):
            cs = [hand[i] for i in combo]
            name, sc = evaluate(cs)
            ch, m, _ = BASE[name]
            s = (ch + sum(CHIPS.get(c["rank"], RANKS[c["rank"]]) for c in sc)) * m
            if need5 and k < 5: continue
            sel = list(combo) if need5 else [i for i in combo if hand[i] in sc]
            if s > best[0]: best = (s, name, sel)
    if need5 and best[0] < 0:  # fewer than 5 playable: play whatever
        return 1, "High Card", list(range(min(5, len(hand))))
    return best


def discard_choice(hand, deck, cur_score, rng, samples=24):
    """Candidate keep-sets (flush chase, pairs, straight window, top cards, current best); pick the best by Monte-Carlo
    over the real deck contents. -> indices to throw, or [] if nothing beats playing now."""
    n = len(hand)
    rk = [RANKS.get(c["rank"], 0) for c in hand]
    cands = []
    for s in sorted({c["suit"] for c in hand if c["suit"]}):
        cands.append({i for i, c in enumerate(hand) if c["suit"] == s})
    rc = Counter(rk)
    cands.append({i for i in range(n) if rc[rk[i]] >= 2})
    cands.append(set(sorted(range(n), key=lambda i: -rk[i])[:3]))
    cands.append(set(best_play(hand)[2]))
    for lo in range(1, 11):
        win = {}
        for i in range(n):
            r = 14 if rk[i] == 1 else rk[i]
            if lo <= r <= lo + 4 or (lo == 1 and r == 14): win.setdefault(r, i)
        if len(win) >= 4: cands.append(set(win.values()))
    best_ev, best_toss = cur_score * 1.15, []
    for keep in cands:
        toss = [i for i in range(n) if i not in keep][:5]
        if not toss: continue
        kept = [hand[i] for i in range(n) if i not in toss]
        tot = 0
        for _ in range(samples):
            tot += best_play(kept + rng.sample(deck, min(len(toss), len(deck))))[0]
        if tot / samples > best_ev: best_ev, best_toss = tot / samples, toss
    return best_toss


def joker_value(text):
    t = " ".join(text)
    v = 1.0
    for x in re.findall(r"X(\d+(?:\.\d+)?) Mult", t): v += (float(x) - 1) * 30
    for x in re.findall(r"\+(\d+) Mult", t): v += int(x) * 1.5
    for x in re.findall(r"\+(\d+) Chips", t): v += int(x) / 10
    if "Earn $" in t: v += 3
    return v


def pack_kind(card):
    return next((k for k in PACK_KINDS if k in card["key"]), card["key"])


class Bot:
    def __init__(self, g, assist=False, blind_cheat=False, skip_small=False, stall=3000, log=None):
        self.g, self.assist, self.blind_cheat, self.skip_small, self.stall, self.log = g, assist, blind_cheat, skip_small, stall, log or (lambda *_: None)
        self.states, self.packs, self.bosses, self.tags = Counter(), Counter(), set(), []
        self.max_ante, self.won, self.over, self.sold, self.rerolls, self.vouchers, self.hands = 0, False, False, 0, 0, 0, 0
        self.shop_key = self.last = None
        self.nstuck = 0
        self.assist_log = []
        self.saved = False
        self.rng = random.Random(1)

    # -- helpers --
    def tick_until(self, cond, n=3000):
        for _ in range(n):
            if cond(): return True
            self.g.tick()
        return False

    def clear_highlight(self, area="hand"):
        for i, c in reversed(list(enumerate(self.g.snapshot()["areas"][area]["cards"]))):
            if c["highlighted"]: self.g.toggle(area, i)

    def try_use(self, area, idx, need_hand=True):
        """use() a consumable/pack card, highlighting 0..3 hand cards for targeted ones."""
        g = self.g
        if g.use(area, idx): return True
        if need_hand:
            n = len(g.snapshot()["areas"]["hand"]["cards"])
            for k in range(min(3, n)):
                g.toggle("hand", k)
                if g.use(area, idx): return True
            self.clear_highlight()
        return False

    def cheat(self, what):
        self.assist_log.append(what)

    # -- per-state handlers (each acts at most once per call) --
    def blind_select(self, s):
        ch = {b["type"]: b for b in s["blind_choices"]}
        cur = next((b for b in s["blind_choices"] if b["state"] == "Select"), None)
        if self.skip_small and cur and cur["type"] == "Small" and cur["tag"] and not self.tags:
            if self.g.skip_blind():
                self.tags.append(cur["tag"]["name"]); return
        if cur and cur["type"] == "Boss": self.bosses.add(cur["name"])
        self.g.select_blind()

    def selecting_hand(self, s):
        g = self.g
        if s["hud"]["blind"] and s["hud"]["blind"]["boss"]: self.bosses.add(s["hud"]["blind"]["name"])
        hand = s["areas"]["hand"]["cards"]
        if not hand: return
        hud = s["hud"]
        # use consumables first (planets, tarots w/ targets)
        for i in range(len(s["areas"]["consumeables"]["cards"])):
            if self.try_use("consumeables", i):
                return
        forced = [i for i, c in enumerate(hand) if c["highlighted"]] if hud["blind"] and hud["blind"]["key"] == "bl_final_bell" else []
        if not forced and any(c["highlighted"] for c in hand): self.clear_highlight(); return
        psychic = hud["blind"] and hud["blind"]["key"] == "bl_psychic"
        sc, name, sel = best_play(hand, need5=bool(psychic))
        if not sel:  # everything debuffed (Verdant Leaf: sell a joker to lift it); else just throw cards away
            jk = s["areas"]["jokers"]["cards"]
            if jk and g.sell("jokers", 0): self.sold += 1; return
            sel = [0]
        remaining = hud["target"] - hud["chips"]
        jok = sum(joker_value(g.describe(c["id"])) for c in s["areas"]["jokers"]["cards"]) if s["areas"]["jokers"]["cards"] else 0
        if self.blind_cheat and hud["hands_left"] == 1 and hud["chips"] < hud["target"] and sc > 0:  # CHEAT: last hand can't lose
            g.G.GAME.blind.chips = hud["chips"] + 1; self.cheat("blind->chips+1")
            remaining = 1
        toss = []
        if not (sc * max(1.0, jok / 3) >= remaining or hud["discards_left"] == 0) and hud["discards_left"] > 0:
            toss = discard_choice(hand, s["areas"]["deck"]["cards"], sc, self.rng)
        def pick(idx):  # idempotent selection (Cerulean Bell keeps one card forced-selected)
            idx = set(idx) | set(forced)
            for i, c in enumerate(hand):
                if (i in idx) != c["highlighted"] and i not in forced: g.toggle("hand", i)
        if toss:
            pick(toss[:5 - len(forced)])
            if g.discard(): return
            self.clear_highlight()
        pick(sel[:5 - len(forced)])
        if g.play(): self.hands += 1; return
        self.clear_highlight()

    def round_eval(self, s):
        self.g.cash_out()

    def shop(self, s):
        g = self.g
        key = (s["hud"]["round"], s["hud"]["ante"])
        if key != self.shop_key:
            self.shop_key, self.shop_rerolls, self.shop_done = key, 0, set()
            if self.assist and s["hud"]["dollars"] < 60:
                g.G.GAME.dollars = 60; self.cheat("dollars->60"); return
        a, d = s["areas"], s["hud"]["dollars"]
        jokers = a["jokers"]["cards"]
        full = len(jokers) >= s["hud"]["joker_slots"]
        # use held consumables (not planets-in-hand only; any) — non-targeted ones work in shop
        for i in range(len(a["consumeables"]["cards"])):
            if self.try_use("consumeables", i, need_hand=False): return
        # vouchers
        for i, c in enumerate(a["shop_vouchers"]["cards"]):
            if c["cost"] <= d - (0 if self.assist else 8) and g.buy("shop_vouchers", i): self.vouchers += 1; return
        # jokers / shop consumables
        vals = [joker_value(g.describe(c["id"])) for c in jokers]
        for i, c in sorted(enumerate(a["shop_jokers"]["cards"]), key=lambda t: -t[1]["cost"]):
            if c["cost"] > d: continue
            if c["set"] == "Joker":
                v = joker_value(g.describe(c["id"]))
                if not full:
                    if g.buy("shop_jokers", i): return
                elif vals and v > min(vals) * 1.5:
                    j = vals.index(min(vals))
                    if g.sell("jokers", j): self.sold += 1; return
            elif c["set"] == "Planet":
                if g.buy("shop_jokers", i, use=True): return
            elif c["set"] == "Tarot" and len(a["consumeables"]["cards"]) < s["hud"]["consumable_slots"]:
                if g.buy("shop_jokers", i): return
        # packs: one of each unseen kind first, then planets
        for i, c in enumerate(a["shop_booster"]["cards"]):
            k = pack_kind(c)
            if c["cost"] <= d - (0 if self.assist else 2) and (self.packs[k] == 0 or k == "celestial") and g.buy("shop_booster", i):
                self.packs[k] += 1; return
        # reroll for jokers when rich
        if self.shop_rerolls < (6 if self.assist else 2) and d - s["shop"]["reroll_cost"] >= (10 if not self.assist else 20) and (not full or self.assist):
            if g.reroll(): self.shop_rerolls += 1; self.rerolls += 1; return
        g.next_round()

    def pack(self, s):
        g = self.g
        cards = s["areas"]["pack_cards"]["cards"]
        order = sorted(range(len(cards)), key=lambda i: -joker_value(g.describe(cards[i]["id"])) if cards[i]["set"] == "Joker" else 0)
        for i in order:
            if self.try_use("pack_cards", i): return
        self.clear_highlight()
        g.skip_pack()

    # -- main loop --
    def step(self):
        g = self.g
        s = g.snapshot()
        st = s["state"]
        self.states[st] += 1
        fp = (st, s["busy"], s["overlay"], s["hud"] and (s["hud"]["dollars"], s["hud"]["chips"], s["hud"]["hands_left"], s["hud"]["discards_left"], s["hud"]["ante"],
              s["hud"]["deck_count"], len(s["areas"]["hand"]["cards"])))
        self.nstuck = self.nstuck + 1 if fp == self.last else 0
        self.last = fp
        if self.nstuck > self.stall:
            raise Hang(f"state={st} fp={fp} blind={s['hud'] and s['hud']['blind']} can={s['can']} hand={[(c['rank'], c['suit'], c['debuff'], c['highlighted'], c['facing']) for c in s['areas']['hand']['cards']]}")
        if s["hud"]: self.max_ante = max(self.max_ante, s["hud"]["ante"])
        if s["won"] and not self.won:
            self.won = True
        if s["won"] and s["overlay"]:
            g.button("exit_overlay_menu"); return "won"
        if st == "GAME_OVER":
            self.over = True; return "over"
        if s["busy"]: return
        if st == "BLIND_SELECT": self.blind_select(s)
        elif st == "SELECTING_HAND": self.selecting_hand(s)
        elif st == "ROUND_EVAL": self.round_eval(s)
        elif st == "SHOP": self.shop(s)
        elif st.endswith("_PACK"):
            k = {"TAROT": "arcana", "PLANET": "celestial", "SPECTRAL": "spectral", "STANDARD": "standard", "BUFFOON": "buffoon"}[st[:-5]]
            self.pack(s)
        return None

    def run(self, max_ticks=400000, until_win=True):
        g = self.g
        for n in range(max_ticks):
            r = self.step()
            if r == "over": return "over"
            if r == "won" and until_win:
                for _ in range(6000):  # endless continue: overlay closes, play on through cash-out into the shop
                    g.tick(); self.step()
                    s = g.snapshot()
                    if s["state"] in ("SHOP", "BLIND_SELECT") and not s["busy"] and not s["overlay"]: return "won"
                raise Hang("no shop after win overlay")
            g.tick()
        raise Hang("max_ticks")


def save_check(data_dir, speed, seed):
    """Play one hand of ante 1, then reload in a fresh Game from the same data_dir and compare."""
    g = Game(data_dir=data_dir, speed=speed)
    g.new_run(seed)
    b = Bot(g)
    for _ in range(20000):
        s = g.snapshot()
        if s["state"] == "SELECTING_HAND" and not s["busy"] and b.hands >= 1: break
        b.step(); g.tick()
    else:
        raise Hang("save_check never reached 2nd hand")
    for _ in range(120): g.tick()  # let the save flush
    s1 = g.snapshot()

    def sig(s):
        h = s["hud"]
        return (h["ante"], h["round"], h["dollars"], h["hands_left"], h["discards_left"], h["chips"], h["target"], h["deck_count"], h["blind"]["key"],
                [(c["key"], c["front"], c["edition"], c["seal"]) for c in s["areas"]["hand"]["cards"]],
                [(c["key"], c["edition"]) for c in s["areas"]["jokers"]["cards"]])

    g2 = Game(data_dir=data_dir, speed=speed)
    assert g2.has_save() and g2.continue_run()
    for _ in range(3000):
        s2 = g2.snapshot()
        if s2["state"] == "SELECTING_HAND" and not s2["busy"]: break
        g2.tick()
    assert sig(s1) == sig(s2), f"save mismatch\n{sig(s1)}\n{sig(s2)}"
    # and the restored game is still playable
    b2 = Bot(g2)
    n0 = s2["hud"]["hands_left"]
    for _ in range(5000):
        b2.step(); g2.tick()
        if g2.snapshot()["hud"]["hands_left"] < n0 or g2.snapshot()["hud"]["ante"] > 1: break
    else:
        raise Hang("restored game did not play")
    return sig(s1)[:9]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--assist", action="store_true", help="CHEAT: top up dollars to 60 at each shop")
    ap.add_argument("--blind-cheat", action="store_true", help="CHEAT (needs --assist): halve boss/blind targets when behind")
    ap.add_argument("--speed", type=float, default=8)
    ap.add_argument("--stall", type=int, default=3000)
    ap.add_argument("--prefix", default="BOT")
    ap.add_argument("--start", type=int, default=0, help="first seed index")
    ap.add_argument("--stake", type=int, default=1)
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="balatro-bot-")
    print("data_dir", tmp, "assist", a.assist, "speed", a.speed)
    bad = 0
    tot = {"packs": Counter(), "bosses": set(), "states": set(), "tags": [], "won": 0, "sold": 0, "rerolls": 0, "vouchers": 0}
    try:
        g = Game(data_dir=tmp, speed=a.speed)  # reused across seeds: also exercises new_run after GAME_OVER / win
        for i in range(a.start, a.start + a.seeds):
            seed = f"{a.prefix}{i}"
            g.new_run(seed, stake=a.stake)
            bot = Bot(g, assist=a.assist, blind_cheat=a.blind_cheat, skip_small=(i % 2 == 1), stall=a.stall)
            res = None
            try:
                res = bot.run()
                if res == "won":  # endless: keep going 1 more ante briefly to prove the continue works
                    s = g.snapshot(); assert s["state"] in ("SHOP", "BLIND_SELECT"), s["state"]
                    bot.over = False
            except Hang as e:
                res = f"HANG {e}"; bad += 1
            except Exception as e:  # noqa
                import traceback; traceback.print_exc()
                res = f"ERROR {type(e).__name__}: {e}"; bad += 1
            print(f"seed {seed}: result={res} furthest_ante={bot.max_ante} won={bot.won} hands={bot.hands} sold={bot.sold} rerolls={bot.rerolls} "
                  f"vouchers={bot.vouchers} skip_tags={bot.tags}\n   packs={dict(bot.packs)}\n   bosses={sorted(bot.bosses)}\n   states={sorted(bot.states)}"
                  + (f"\n   assist={Counter(bot.assist_log)}" if bot.assist_log else ""), flush=True)
            tot["packs"] += bot.packs; tot["bosses"] |= bot.bosses; tot["states"] |= set(bot.states); tot["tags"] += bot.tags
            tot["won"] += bot.won; tot["sold"] += bot.sold; tot["rerolls"] += bot.rerolls; tot["vouchers"] += bot.vouchers
        try:
            print("save/continue check OK:", save_check(tmp, a.speed, a.prefix + "SAVE"))
        except Exception as e:  # noqa
            import traceback; traceback.print_exc()
            print("save/continue check FAILED:", e); bad += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"TOTAL wins={tot['won']}/{a.seeds} packs={dict(tot['packs'])} bosses={len(tot['bosses'])} sold={tot['sold']} rerolls={tot['rerolls']} "
          f"vouchers={tot['vouchers']} tags={tot['tags']}\n states={sorted(tot['states'])}\n bosses={sorted(tot['bosses'])}")
    print("HANGS/ERRORS:", bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
