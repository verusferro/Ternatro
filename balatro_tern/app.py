"""Tern session, tick loop, input -> actions, render."""
import json
import threading
import os
import signal
import sys
import time
from pathlib import Path

import tern_sdk
from tern_sdk import ui

from .motion import Motion, build_sheets, drift_css, units
from .sound import Sound

from . import view
from .game import DATA_DIR, Game
from .input import Input

from . import shaders
from .scene import Scene

CSS = "".join(Path(__file__).with_name(n).read_text() for n in ("style.css", "motion.css", "scene.css"))
DT = 1 / 60
IDLE_SHEET_DT = 1 / 8


def title(snap):
    """Pane title `Balatro: Ante 3 · $14`. Tern labels a parked pane with the word before ':' (omp's 'TAG: ...'
    convention); the plugin's status line reads the rest."""
    h = snap.get("hud")
    if not h:
        return "Balatro"
    return f"Balatro: Ante {h['ante']} · ${h['dollars']}"


def render_all(sf, snap, game, ctx):
    """Rebuild the node tree: backdrop + the room (Scene boxes, cards, popups)."""
    # background.fs mosaics (shaders.bg_tiles), placed by the `bgtiles` sheet: the current state's, plus the likely next
    # ones hidden (.bgpre), already decoded by Tern so a state change shows its swirl at once (start_bakes)
    swirl = ui.html.div(*[ui.html.div(*[ui.html.div(ui.image(b, alt=""), class_=f"bgt bgt{i}", key=f"bgt{i}") for i, b in enumerate(ids)],
                                      class_="bgm" if st == ctx.bg_cur else "bgm bgpre", key=f"bgm-{st[0]}{st[1] or ''}")
                          for st, ids in ctx.bgs.items()], class_="swirl", key="swirl")
    stage = ui.html.div(swirl, ui.html.div(view.table(snap, game, ctx), class_="fg", key="fg"), class_="stage", key="stage")
    if ctx.bg:
        css = bg_css(ctx)
        if css != getattr(ctx, "bg_css_sent", None):
            ctx.bg_css_sent = css
            sf.stylesheet("bgtiles", css)
    sf.render(main=ui.col(stage))
    # popups of the cards the player can reach: the overlay's own (deck view) while it shows any, else the table's
    # (every popup's rules count against Tern's 256 KiB of sheets per surface)
    cards = ctx.fr.cards if ctx.fr else ()
    ids = {c["id"] for c in cards if c["area"] == "misc"} or {c["id"] for c in cards}
    ctx.scene.tips_sheet(sf, ids)


def bg_css(ctx):
    """Placement of the swirl mosaic: tiles in percent of `.bgm`, which is the stage's height and at least its width,
    at least as wide as the bake's aspect wants (centred, the sides cropped).  The swirl is only baked for the wide
    stage (the pane's full width): there it fills, on the tall stage it covers, sides cropped.  Tern paints every raster image with a
    6px corner radius and a faint ring, so tiles overlap by 8px and the mosaic bleeds 8px past the stage's edges."""
    W, H = ctx.bg_rects[-1][5], ctx.bg_rects[-1][6]
    return (f".bgm{{width:max(calc(100% + 16px),{ctx.H * W / H:.4f}em)}}" +
            "".join(f".bgt{i}{{left:{t[3] / W * 100:.4f}%;top:{t[4] / H * 100:.4f}%;width:calc({(t[5] - t[3]) / W * 100:.4f}% + 8px);"
                    f"height:calc({(t[6] - t[4]) / H * 100:.4f}% + 8px)}}" for i, t in enumerate(ctx.bg_rects)))


def start_bakes(session, ctx, snap, game, state):
    """Swirls: the current state's (baked on every core if it isn't on disk yet) and the likely next ones (view.bg_preds,
    only when on disk) are loaded on threads, sent one tile per loop, and kept in ctx.bgs; others are dropped."""
    want = view.bg_name(snap, game)
    keep = [want] + [s for s in view.bg_preds(snap, game) if s != want]
    jobs = state.setdefault("jobs", {})  # state -> [tiles or None, blob ids sent]
    for st in [s for s in jobs if s not in keep and jobs[s][0]]:  # loaded for a prediction that no longer holds
        del jobs[st]

    def load(st):
        try:
            jobs[st][0] = shaders.bg_tiles(*st, ctx.bg_size, workers=max(1, (os.cpu_count() or 2) - 2))
        except Exception as e:  # keep the CSS swirl
            state["err"] = repr(e)
    for st in keep:  # predictions not baked yet are skipped (bg_prebake bakes them)
        if st not in ctx.bgs and st not in jobs and (st == want or shaders.bg_cached(*st, ctx.bg_size)):
            jobs[st] = [None, []]
            threading.Thread(target=load, args=(st,), daemon=True).start()
    for st in keep:  # one tile per loop, the current state's first: a state's tiles are megabytes, at once they would stall a frame
        job = jobs.get(st)
        if job and job[0]:
            tiles, ids = job
            if len(ids) < len(tiles):
                t = tiles[len(ids)]
                ids.append(session.blob(t[1], t[2]))
                break
            del jobs[st]
            ctx.bgs[st], ctx.bg_rects = ids, tiles
            ctx.dirty = True
    if want in ctx.bgs and ctx.bg_cur != want:
        ctx.bg_cur, ctx.bg = want, ctx.bgs[want]
        ctx.dirty = True
    for st in [s for s in ctx.bgs if s not in keep and s != ctx.bg_cur]:  # the shown one stays until its successor is up
        del ctx.bgs[st]
        ctx.dirty = True


def debug(game, scene=None):
    """BALATRO_DEBUG='joker=j_joker,j_greedy_joker big options' : testing aids (jokers, tiny blind + huge hand scores
    for flames, force a screen: win | over | options)."""
    import os
    dbg = os.environ.get("BALATRO_DEBUG", "")
    if not dbg:
        return
    for _ in range(300):
        game.tick()
    for tok in dbg.split():
        if tok.startswith("joker="):
            for key in tok[6:].split(","):
                game.lua.execute(f"local c = create_card('Joker', G.jokers, nil, nil, nil, nil, '{key}'); c:add_to_deck(); G.jokers:emplace(c)")
        elif tok in ("win", "over", "options") and scene is not None:
            scene.force(game, tok)
        elif tok == "specials":  # one of each enhancement / edition / seal on the first deck cards, to check the deck view faces
            game.lua.execute("local P, i = G.playing_cards, 0 local function nx() i = i + 1 return P[i] end "
                             "for _, m in ipairs{'bonus','mult','wild','glass','steel','stone','gold','lucky'} do nx():set_ability(G.P_CENTERS['m_' .. m]) end "
                             "for _, e in ipairs{'foil','holo','polychrome'} do nx():set_edition({[e] = true}, true, true) end "
                             "for _, s in ipairs{'Gold','Red','Blue','Purple'} do nx():set_seal(s, true, true) end "
                             "local c = nx() c:set_ability(G.P_CENTERS.m_glass) c:set_edition({holo = true}, true, true) c:set_seal('Red', true, true) "
                             "nx():set_base(G.P_CARDS.H_K) nx():set_base(G.P_CARDS.S_A)")
        elif tok == "big":
            game.lua.execute("for _, h in pairs(G.GAME.hands) do h.mult = h.mult * 40; h.chips = h.chips * 20 end G.GAME.blind.chips = 1; G.GAME.blind.chip_text = '1'")


def main(continue_run=False, seed=None, speed=2.0):
    if sys.platform == "win32":  # ConPTY decodes our bytes with the console code page (OEM): the "·" in the title would arrive as mojibake
        import atexit, ctypes
        k32 = ctypes.windll.kernel32
        old = k32.GetConsoleOutputCP()
        k32.SetConsoleOutputCP(65001)
        atexit.register(k32.SetConsoleOutputCP, old)
    session = tern_sdk.connect(app="balatro")
    if session is None:
        raise SystemExit("balatro-tern needs Tern (TSP unavailable): run it inside a Tern pane.")
    game = Game(speed=speed)
    errlog = open(Path(DATA_DIR) / "errors.log", "a")
    snd = Sound()

    blob_file = Path(DATA_DIR) / "blobs.txt"  # ids sent to Tern, which keeps blobs on disk across program restarts

    def save_blobs():
        blob_file.write_text("\n".join(session._blobs_sent))

    def hangup(signum, _frame):  # pane closed / killed: the pty is gone, so skip the SDK's goodbye writes, stop the sound now
        if snd.proc:
            snd.proc.kill()
        save_blobs()
        os._exit(128 + signum)
    for name in ("SIGHUP", "SIGTERM", "SIGINT", "SIGBREAK"):  # SIGHUP is Unix-only, SIGBREAK Windows-only
        if (s := getattr(signal, name, None)) is not None:
            signal.signal(s, hangup)
    with session:
        sf = session.open(mode="screen", title="Balatro")
        sent = {"n": 0, "b": 0, "by": {}}
        if os.environ.get("BALATRO_DEBUG"):  # count every stylesheet verb for the debug log
            raw_sheet = sf.stylesheet

            def counted(name, css, *a, **k):
                sent["n"] += 1
                sent["b"] += len(css or "")
                sent["by"][name] = sent["by"].get(name, 0) + len(css or "")
                return raw_sheet(name, css, *a, **k)
            sf.stylesheet = counted
        sf.stylesheet("balatro", CSS)
        if session.caps.has("blobs") and blob_file.exists():  # skip resending what Tern still holds from earlier runs
            ids = blob_file.read_text().split()
            for i in range(0, len(ids), 2000):  # ~135 KB per query, under the 256 KiB APC cap
                session._blobs_sent.update(session.blobs(ids[i:i + 2000]))
        ctx = view.Ctx(session)
        cols, cell = session.caps.cols, session.caps.cell
        if cols:
            ctx.resize(cols, cell)
        prefs = Path(DATA_DIR) / "prefs.json"
        try:
            p = json.loads(prefs.read_text())
            snd.mute_music, snd.mute_sfx = bool(p.get("mute_music")), bool(p.get("mute_sfx"))
        except (OSError, ValueError):
            pass

        def toggle(kind):
            muted = snd.toggle_music() if kind == "music" else snd.toggle_sfx()
            prefs.write_text(json.dumps({"mute_music": snd.mute_music, "mute_sfx": snd.mute_sfx}))
            errlog.write(f"prefs: {kind} muted={muted} music={snd.mute_music} sfx={snd.mute_sfx}\n")
            errlog.flush()
            ctx.redraw()
        ctx.toggle = toggle
        motion = Motion(game)
        inp = ctx.input = Input(game, redraw=ctx.redraw)
        scene = ctx.scene = Scene(game, session, click=lambda uid: inp.on_click("uie", uid))
        for kind, k in (("c", "chips"), ("m", "mult")):
            def mk(kind=kind, k=k):
                f = ctx.flames.get(kind)
                if not f:
                    return None
                return ui.image(f[1], w=round(2.5 * ctx.Upx), h=round(2.5 * ctx.Upx), alt="", class_="flimg", key=f"fl{kind}{f[0]}")
            scene.flame_content[kind] = mk
        bake_state, flame_jobs = {}, {}
        scene.on_toggle = toggle
        scene.set_mute(snd.mute_music, snd.mute_sfx)
        if not (continue_run and game.continue_run()):  # after Scene: its UI overrides (HUD, Options) must exist first
            game.new_run(seed)
        debug(game, scene)
        ctx.ox, ctx.oy = motion.room_orig()
        W, H = ctx.wide = ctx.W, ctx.H  # the wide layout's stage: the swirl is baked for it, the tall stage shows it cropped (bg_css)
        ln = (W ** 2 + H ** 2) ** .5  # background.fs's grid: 700 cells along the diagonal, one px per cell
        ctx.bg_size = round(700 * W / ln), round(700 * H / ln)
        bosses = sorted({"#" + view.boss_colour({"hud": {"blind": {"key": k}}}, game) for k, b in game.G.P_BLINDS.items() if b.boss})
        jobs = [(n, None, ctx.bg_size) for n in ("small", "tarot", "planet", "buffoon", "standard", "spectral", "won")]

        def prebake(jobs):  # after the first swirl is up (at most a minute): that one bakes first, on every core
            for _ in range(120):
                if ctx.bg is not None:
                    break
                time.sleep(.5)
            shaders.bg_prebake(jobs)
        threading.Thread(target=prebake, args=(jobs + [("boss", c, ctx.bg_size) for c in bosses],), daemon=True).start()
        ctx.resize(cols, cell)
        last, last_title, running, shown = None, None, True, True
        last_key, last_scene, last_sheets, layout = None, None, {}, None
        t_sheet, last_drift = 0.0, None
        stats = {"t": time.monotonic(), "sheets": 0, "frames": 0, "renders": 0, "cpu": time.process_time()}
        t_prev = t_next = time.monotonic()
        owed = 0.0
        tty = None  # rows can change after the TSP resize event (SIGWINCH races it): re-fit on either
        while running and not session.closed and not session._eof:  # _eof: tty EOF/EIO (pane or Tern gone)
            # wait only until the next 1/60 s slot (not a full DT after the work): the loop then runs at 60 Hz with one game tick each, so a slide's per-send steps are even
            item = session.poll(max(0.0, t_next - time.monotonic()))
            if time.monotonic() >= t_next:
                t_next = max(t_next + DT, time.monotonic() - DT)
            while item is not None:
                if hasattr(item, "ctrl"):  # Ctrl+C quits (the run is saved); a/d/w/s reorder the last clicked card
                    if item.ctrl and item.name == "c":
                        running = False
                        break
                    if not (item.ctrl or item.alt or item.meta):
                        inp.key(item.name)
                elif type(item).__name__ in ("ResizeEvent", "VisibleEvent"):
                    if item.visible is not None and item.visible != shown:  # hidden tab: the game runs on, nothing is drawn
                        shown = item.visible
                        ctx.dirty = True
                    if type(item).__name__ == "ResizeEvent":
                        ctx.resize(item.cols, item.cell)
                elif type(item).__name__ == "ErrorEvent":
                    errlog.write(f"{item.sheet}: {item.msg}\n")
                    errlog.flush()
                item = session.poll(0)
            try:
                size = os.get_terminal_size()
            except OSError:
                size = None
            if size != tty:
                tty = size
                ctx.resize(session.caps.cols, session.caps.cell)
            now = time.monotonic()
            owed = min(owed + now - t_prev, 4 * DT)
            t_prev = now
            if ctx.mute != (snd.mute_music, snd.mute_sfx):
                ctx.mute = (snd.mute_music, snd.mute_sfx)
                scene.set_mute(*ctx.mute)
                ctx.dirty = True
            while owed >= DT * 0.9:  # a wake a hair early still ticks: otherwise jitter turns 1,1,1 ticks per loop into 0,2,1
                inp.pre_tick()
                game.tick(DT)
                owed -= DT
            feed = game.drain_feed()
            for e in feed:
                snd.handle(e)
            if not shown:
                continue
            if ctx.want_tall != ctx.tall and scene.SC.set_layout(ctx.want_tall):  # false while the game is busy: retried next loop
                ctx.apply_layout(ctx.want_tall, *motion.room_orig())
            fr = motion.read()
            scene.read()
            if fr and fr.flames and last:
                for kind, k, real in (("c", "chips", fr.flames[0]), ("m", "mult", fr.flames[2])):
                    lvl = shaders.flame_level(real)
                    cur = ctx.flames.get(kind)
                    if lvl is None:
                        if cur:
                            ctx.flames.pop(kind)
                            ctx.dirty = True
                    elif not cur or cur[0] != lvl:
                        key = (kind, lvl)
                        if key not in flame_jobs:
                            flame_jobs[key] = None

                            def fjob(key=key, k=k):
                                flame_jobs[key] = shaders.flame_bake(k, key[1])[1:]
                            threading.Thread(target=fjob, daemon=True).start()
                        if flame_jobs.get(key) is not None:
                            if not isinstance(flame_jobs[key], str):
                                flame_jobs[key] = session.blob(*flame_jobs[key])
                            ctx.flames[kind] = (lvl, flame_jobs[key])
                            ctx.dirty = True
            if last:
                start_bakes(session, ctx, last, game, bake_state)
            # heavy snapshot only when cards / HUD scalars changed; the node tree also when Scene's structure or focus changed
            if ctx.dirty or fr is None or fr.key != last_key or last is None:
                last = game.snapshot()
            if ctx.dirty or fr is None or fr.key != last_key or scene.key != last_scene:
                ctx.fr = fr
                if layout != (ctx.fit, ctx.W, ctx.H):
                    layout = ctx.fit, ctx.W, ctx.H
                    sf.stylesheet("layout", view.layout_css(ctx))
                ctx.dirty = False
                last_key, last_scene = (fr.key if fr else None), scene.key
                render_all(sf, last, game, ctx)
                stats["renders"] += 1
                t = title(last)
                if t != last_title:
                    last_title = t
                    session.write(f"\x1b]2;{t}\x07".encode())
            if fr:
                ctx.fr = fr
                drift = drift_css(round(fr.shake, 3))
                if drift != last_drift:
                    last_drift = drift
                    sf.stylesheet("drift", drift)
                sheets = build_sheets(fr, ctx.U)
                t_now = time.monotonic()
                # idle sway (hand cards bobbing) only goes out at 8 Hz (its steps are 0.25 deg / 1 css px at most): each sheet costs Tern a full restyle + frame
                if sheets != last_sheets and (fr.moving or t_now - t_sheet >= IDLE_SHEET_DT):
                    t_sheet = t_now
                    for name in last_sheets.keys() - sheets.keys():
                        sf.stylesheet(name, None)
                    for name, css in sheets.items():
                        if last_sheets.get(name) != css:
                            sf.stylesheet(name, css)
                    last_sheets = sheets
                    stats["sheets"] += 1
                scene.update(sf, units(ctx, fr))
            stats["frames"] += 1
            if os.environ.get("BALATRO_DEBUG") and time.monotonic() - stats["t"] >= 5:
                dt = time.monotonic() - stats["t"]
                with open("/tmp/balatro-motion-stats.log", "a") as lf:
                    lf.write(f"[{os.getpid()}] loop {stats['frames'] / dt:.1f}/s  motion sheets {stats['sheets'] / dt:.1f}/s  "
                             f"renders {stats['renders'] / dt:.1f}/s  sheet verbs {sent['n'] / dt:.1f}/s {sent['b'] / dt / 1024:.1f} KiB/s  "
                             f"cpu {(time.process_time() - stats['cpu']) / dt * 100:.0f}%\n")
                stats.update(t=time.monotonic(), sheets=0, frames=0, renders=0, cpu=time.process_time())
                top = sorted(sent["by"].items(), key=lambda kv: -kv[1])[:4]
                with open("/tmp/balatro-motion-stats.log", "a") as lf:
                    lf.write(f"[{os.getpid()}]   top sheets B/s: " + " ".join(f"{k}={v / dt:.0f}" for k, v in top) + "\n")
                sent.update(n=0, b=0, by={})
        snd.close()
        save_blobs()
