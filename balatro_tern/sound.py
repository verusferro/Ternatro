"""Balatro audio: a Python port of engine/sound_manager.lua (SOURCES, PLAY_SOUND, SET_SFX, MODULATE,
RESTART_MUSIC, AMBIENT) driven by the exact requests the game pushes to its 'sound_request' channel
(stubs.lua turns them into feed entries). Mixed with numpy and streamed into one persistent ffmpeg->pulse process.

Feed entries consumed by Sound.handle():
  {"kind":"sound","name","pitch","volume","vols":[master,music,game]}
  {"kind":"modulate","track","pitch_mod","dt","ambient":{name:{"vol","per"}},"vols":[...]}
Everything else is ignored.  Run `python -m balatro_tern.sound` for the self-check.
"""
import glob, logging, os, shutil, subprocess, sys, threading, time
from pathlib import Path
import numpy as np
from .game import DATA_DIR

log = logging.getLogger("balatro.sound")
RATE, BLOCK, LEAD = 44100, 512, 0.08  # 11.6 ms per block; Windows path keeps 80 ms queued (rides out GIL stalls)
ROOT = Path(__file__).resolve().parent.parent / "extracted"
CACHE = Path(DATA_DIR) / "pcm"
F_SETPIPE_SZ = 1031
WSL = sys.platform == "linux" and os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
PLAYER = ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32",
          "-analyzeduration", "0", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
          "-f", "pulse", "-buffer_duration", "30", "-name", "Balatro", "balatro"]


class Voice:
    __slots__ = ("code", "data", "pos", "pitch", "vol", "opitch", "ovol", "cur", "playing", "g", "pa")

    def __init__(self, code, data, opitch, ovol):
        self.code, self.data, self.pos = code, data, 0.0
        self.pitch, self.vol, self.opitch, self.ovol, self.cur, self.playing = opitch, 0.0, opitch, ovol, None, True
        self.g = self.pa = None  # gain / pitch applied at the end of the previous block


class Sound:
    def __init__(self, enabled=True, root=ROOT, cmd=None, probe=False):
        self.lock = threading.Lock()
        self.mute_music = False
        self.mute_sfx = False
        self.vols = (50, 100, 100)
        self.pcm = {}          # code -> int16 (n,2) memmap, filled by the warm-up thread
        self.proc = None
        self.writes = 0        # blocks written to the player
        self.backend = None    # "windows" | "pulse" | "native" | "custom" once started
        self.winlog = []       # stderr lines from winplay.py (probe: "onset <ms>", "stats starved=N flagged=N")
        self.late = 0          # times the Windows player ran out of queued audio
        self._pos = None       # (bytes the player consumed, perf_counter when reported) from winplay's `pos` lines
        self.underflows = 0    # native backend: PortAudio callbacks flagged underflow
        self._cmd, self._probe = cmd, probe
        self.stream = None     # sounddevice stream (native backend)
        self.enabled = False
        files = sorted(Path(root, "resources/sounds").glob("*.ogg"))
        self.codes = [f.stem for f in files]
        # SOURCES: music keys exist from the start (empty list => MODULATE restarts them), like the Lua thread
        self.src = {c: [] for c in self.codes if "music" in c}
        if not enabled or not files:
            return
        self.enabled = True
        self._files = files
        threading.Thread(target=self._warm, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()

    # ---- decode cache ----
    def _warm(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        for f in sorted(self._files, key=lambda f: "music" in f.stem):  # sfx first
            try:
                out = CACHE / (f.stem + ".s16")
                if not out.exists() or out.stat().st_mtime < f.stat().st_mtime:
                    import soundfile as sf
                    d, sr = sf.read(f, dtype="float32", always_2d=True)
                    d = d[:, [0, 0]] if d.shape[1] == 1 else d[:, :2]
                    if sr != RATE:  # ponytail: linear resample; game ships 44.1 kHz so this never runs in practice
                        x = np.arange(int(len(d) * RATE / sr)) * (sr / RATE)
                        d = np.stack([np.interp(x, np.arange(len(d)), d[:, c]) for c in (0, 1)], 1)
                    tmp = out.with_suffix(".tmp")
                    tmp.write_bytes(np.rint(np.clip(d * 32768, -32768, 32767)).astype(np.int16).tobytes())
                    tmp.replace(out)
                if out.stat().st_size >= 8:
                    self.pcm[f.stem] = np.memmap(out, np.int16, "r").reshape(-1, 2)
            except Exception as e:
                log.warning("decode %s failed: %s", f.name, e)

    def ready(self):
        return len(self.pcm) == len(self.codes)

    # ---- port of sound_manager.lua ----
    def _set_sfx(self, v, a):
        master, music, game = a["vols"]
        if "music" in v.code:
            tgt = 1.0 if v.code == a.get("track") else 0.0
            k = a.get("dt", 0) * 3
            v.cur = tgt * k + (1 - k) * (tgt if v.cur is None else v.cur)
            v.vol = v.cur * v.ovol * master / 100 * music / 100
            v.pitch = v.opitch * a.get("pitch_mod", 1)
        else:
            v.pitch = v.opitch
            sv = v.ovol * master / 100 * game / 100
            if sv <= 0:
                v.playing = False
            else:
                v.vol = sv

    def _play(self, code, per, vol, a):
        data = self.pcm.get(code)
        if data is None:
            return None
        v = Voice(code, data, 1.0 if per is None else per, 1.0 if vol is None else vol)
        self.src.setdefault(code, []).append(v)
        self._set_sfx(v, a)
        return v

    def _restart_music(self, a):
        if not all(c in self.pcm for c in self.src if "music" in c):
            return  # stems not decoded yet; retry next modulate so they all start in sync
        for k in [k for k in self.src if "music" in k]:
            for v in self.src[k]:
                v.playing = False
            self.src[k] = []
            self._play(k, 0.7, 0.6, a)

    def _modulate(self, a):
        if a.get("track"):
            for k, lst in self.src.items():
                if "music" in k and not (lst and lst[0].playing):
                    self._restart_music(a)
                    break
        for lst in self.src.values():
            lst[:] = [v for v in lst if v.playing]
            for v in lst:
                self._set_sfx(v, a)
        master, _, game = a["vols"]
        for k, amb in (a.get("ambient") or {}).items():
            vol, per = amb.get("vol") or 0, amb.get("per") or 1
            start = vol * master / 100 * game / 100 > 0
            for v in self.src.get(k, []):
                if v.playing:
                    v.ovol = vol
                    self._set_sfx(v, a)
                    start = False
            if start:
                self._play(k, per, vol, a)

    def handle(self, e):
        """Never raises, never blocks (lock is held for microseconds)."""
        if not self.enabled or not isinstance(e, dict):
            return
        kind = e.get("kind")
        if kind not in ("sound", "modulate"):
            return
        try:
            a = dict(e)
            a["vols"] = tuple(e.get("vols") or self.vols)
            with self.lock:
                self.vols = a["vols"]
                if kind == "sound":
                    self._play(e.get("name"), e.get("pitch"), e.get("volume"), a)
                else:
                    self._modulate(a)
        except Exception as ex:
            log.warning("sound event failed: %r", ex)

    def mute(self, on=None):
        """Mute/unmute everything; returns the new state."""
        on = not (self.mute_music and self.mute_sfx) if on is None else on
        self.mute_music = self.mute_sfx = on
        return on

    def toggle_music(self):
        self.mute_music = not self.mute_music
        return self.mute_music

    def toggle_sfx(self):
        self.mute_sfx = not self.mute_sfx
        return self.mute_sfx

    # ---- mixer ----
    def _mix(self):
        out = np.zeros((BLOCK, 2), np.float32)
        ar = np.arange(BLOCK, dtype=np.float64)
        ramp = (ar / BLOCK).astype(np.float32)[:, None]
        with self.lock:
            for lst in self.src.values():
                for v in lst:
                    if not v.playing:
                        continue
                    # muted voices keep advancing so music stems stay in sync
                    music = "music" in v.code
                    silent = self.mute_music if music else self.mute_sfx
                    # gain and pitch glide linearly across the block: 60 Hz modulate steps would otherwise click/warble
                    g1 = 0.0 if silent or v.vol <= 1e-4 else v.vol
                    g0 = g1 if v.g is None else v.g
                    v.g = g1
                    p1 = max(v.pitch, 0.01)
                    p0 = p1 if v.pa is None else v.pa
                    v.pa = p1
                    idx = v.pos + p0 * ar + (p1 - p0) * ar * ar / (2 * BLOCK)
                    v.pos += BLOCK * (p0 + p1) / 2
                    n = len(v.data)
                    if music:  # loops seamlessly, like the game's looping sources
                        idx %= n
                        v.pos %= n
                    if (g0 > 0 or g1 > 0) and idx[0] < n - 1:
                        i0 = idx.astype(np.intp)
                        fr = (idx - i0)[:, None].astype(np.float32)
                        i1 = i0 + 1
                        if music:
                            i1 %= n
                            m = slice(None)
                        else:
                            m = i0 < n - 1
                            i0, i1, fr = i0[m], i1[m], fr[m]
                        a, b = v.data[i0].astype(np.float32), v.data[i1].astype(np.float32)
                        gain = g0 + (g1 - g0) * ramp
                        out[: len(i0)] += (a + (b - a) * fr) * gain[: len(i0)]
                    if not music and v.pos >= n - 1:
                        v.playing = False
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()

    def _candidates(self):
        if self._cmd:
            yield "custom", self._cmd, False
            return
        mode = os.environ.get("BALATRO_AUDIO")
        if not WSL or mode == "native":
            yield "native", None, False
            return
        py = next(iter(glob.glob("/mnt/c/Users/*/AppData/Local/balatro-tern/venv/Scripts/python.exe")), None)
        if py and mode != "pulse":
            dest = Path(py).parents[2] / "winplay.py"
            src = Path(__file__).with_name("winplay.py")
            try:
                if not dest.exists() or dest.read_bytes() != src.read_bytes():
                    shutil.copyfile(src, dest)
                win = "C:\\" + str(dest)[7:].replace("/", "\\")  # /mnt/c/... -> C:\...
                yield "windows", [py, "-u", win] + (["--probe"] if self._probe else []), True
            except Exception as e:
                log.info("windows backend unavailable: %s", e)
        yield "pulse", PLAYER, False

    def _start_native(self):
        try:
            from .winplay import open_stream
            buf = bytearray()

            def cb(out, frames, t, status):
                n = frames * 4
                if status:
                    self.underflows += 1
                while len(buf) < n:
                    buf.extend(self._mix())
                    self.writes += 1
                out[:n] = bytes(buf[:n])
                del buf[:n]
            self.stream = open_stream(cb, latency=0.08)  # mixing runs under the GIL: ~80 ms rides out main-thread stalls
            self.stream.start()
            return True
        except Exception as e:
            log.warning("native audio failed: %s", e)
            return False

    def _start(self, name, cmd):
        if name == "native":
            return self._start_native()
        import fcntl, select  # WSL bridge only (Unix pipes)
        try:
            self.proc = p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                             preexec_fn=(lambda: __import__("ctypes").CDLL(None).prctl(1, 9)) if sys.platform == "linux" else None)  # PR_SET_PDEATHSIG, SIGKILL
            fcntl.fcntl(p.stdin.fileno(), F_SETPIPE_SZ, 4096)  # keep the pipe from adding latency
            if name == "windows":  # wait for the 'ready' line, else fall back
                line = p.stderr.readline() if select.select([p.stderr], [], [], 8)[0] else b""
                if b"ready" not in line:
                    p.kill()
                    return False
                self.winlog.append(line.decode(errors="replace").strip())
                threading.Thread(target=self._winread, args=(p,), daemon=True).start()
            return True
        except Exception as e:
            log.info("%s backend failed: %s", name, e)
            return False

    def _winread(self, p):
        for l in p.stderr:
            if l.startswith(b"pos "):
                self._pos = (int(l[4:]), time.perf_counter())
            else:
                self.winlog.append(l.decode(errors="replace").strip())

    def _run(self):
        paced = False
        for name, cmd, paced in self._candidates():
            if self._start(name, cmd):
                self.backend = name
                break
        else:
            self.enabled = False
            log.warning("audio disabled: no working player")
            return
        if name == "native":  # the stream callback mixes; nothing to feed
            return
        k, sent, dry = 0, 0, False
        lead, bps = int(LEAD * RATE) * 4, RATE * 4
        debug = os.environ.get("BALATRO_DEBUG")
        try:
            while self.enabled:
                if paced:
                    # Pace on the player's consumption, not our clock: WSL2's clock runs ~3% fast vs Windows', which
                    # overfilled winplay's queue (latency creep, then a chopped-off burst = pop) when paced on wall time.
                    c, tc = self._pos or (0, None)  # no report yet: nothing consumed
                    queued = sent - (0 if tc is None else min(c + (time.perf_counter() - tc) * bps, sent))
                    if queued + BLOCK * 4 > lead and sent:
                        time.sleep((queued + BLOCK * 4 - lead) / bps)
                        continue
                    if queued <= 0 and k > 16 and not dry:  # player ran dry (GIL/scheduler stall), count it once
                        self.late += 1
                    dry = queued <= 0
                self.proc.stdin.write(self._mix())
                sent += BLOCK * 4
                self.writes += 1
                k += 1
                if debug and k % 430 == 0:  # ~5 s
                    st = next((l for l in reversed(self.winlog) if l.startswith("stats")), "stats starved=0 flagged=0")
                    with open("/tmp/balatro-audio-stats.log", "a") as f:
                        f.write("t=%.0f late=%d %s\n" % (time.time(), self.late, st))
        except Exception as e:
            err = b""
            try:
                err = self.proc.stderr.read()
            except Exception:
                pass
            if self.enabled:
                self.enabled = False
                log.warning("audio disabled (%r) %s", e, err.decode(errors="replace").strip()[-300:])

    def close(self):
        self.enabled = False
        if self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(2)
            except Exception:
                self.proc.kill()


# ---- self-check: BALATRO_AUDIO=pulse forces the WSLg fallback ----
MOD = {"kind": "modulate", "track": "music1", "pitch_mod": 1, "dt": 1 / 60, "ambient": {}}
SFX = (("chips1", 0.8), ("multhit1", 1.0), ("coin1", 1.4))


def _check_windows(s):
    """Player runs with --probe: it reports stdin-read -> DAC time for every silence->sound edge."""
    wr, orig = [], s._mix

    def mix():  # wall time the first non-silent block is handed to the player
        b = orig()
        if any(b):
            wr.append(time.perf_counter())
        return b
    s._mix = mix
    time.sleep(1.5)
    lat = []
    for name, pitch in SFX * 2:
        wr.clear()
        n0, t0 = len(s.winlog), time.perf_counter()
        s.handle({"kind": "sound", "name": name, "pitch": pitch, "volume": 1})
        time.sleep(0.7)
        on = [float(l.split()[1]) for l in s.winlog[n0:] if l.startswith("onset")]
        assert wr and on, "no audio reached the Windows player for %s" % name
        lat.append((wr[0] - t0) * 1000 + on[0])
        print("%-9s pitch %.1f: mixer %.1f ms + player read->DAC %.1f ms = %.0f ms" % (name, pitch, (wr[0] - t0) * 1000, on[0], lat[-1]))
    print("end-to-end handle()->DAC: median %.0f ms (min %.0f, max %.0f); device: %s" % (sorted(lat)[len(lat) // 2], min(lat), max(lat), s.winlog[0]))
    n0, w0 = len(s.winlog), s.writes
    m0 = time.time()
    while time.time() - m0 < 2:
        s.handle(MOD)
        time.sleep(1 / 60)
    assert s.writes - w0 > 100 and any(l.startswith("onset") for l in s.winlog[n0:]), "music didn't reach the player"
    print("music1: blocks written +%d in 2 s (expect ~172); player reported onset" % (s.writes - w0))


def _check_pulse(s):
    """Capture the default sink's monitor and time handle() -> first captured sound."""
    sink = subprocess.run(["ffmpeg", "-hide_banner", "-sources", "pulse"], capture_output=True, text=True).stdout
    mon = next(l.split()[0] for l in sink.splitlines() if ".monitor" in l)
    cap = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-f", "pulse", "-fragment_size", "1024", "-i", mon,
                            "-f", "s16le", "-ar", str(RATE), "-ac", "1", "pipe:1"], stdout=subprocess.PIPE, bufsize=0)
    chunks = []  # (arrival time, rms)

    def rd():
        while True:
            b = cap.stdout.read(1024)
            if not b:
                return
            x = np.frombuffer(b, np.int16).astype(np.float32)
            chunks.append((time.time(), float(np.sqrt((x * x).mean()))))
    threading.Thread(target=rd, daemon=True).start()
    time.sleep(1.0)
    base = max(r for _, r in chunks) if chunks else 0
    t0 = time.time()
    s.handle({"kind": "sound", "name": "chips1", "pitch": 1.0, "volume": 1})
    time.sleep(0.6)
    on = next((a for a, r in chunks if a >= t0 and r > base * 3 + 5), None)
    print("end-to-end latency (handle -> captured on monitor) ~ %s ms" % (None if on is None else round((on - t0) * 1000)))
    for name, pitch in SFX:
        s.handle({"kind": "sound", "name": name, "pitch": pitch, "volume": 1})
        time.sleep(0.5)
    w0 = s.writes
    m0, peak = time.time(), 0
    while time.time() - m0 < 2:
        s.handle(MOD)
        time.sleep(1 / 60)
        peak = max([r for a, r in chunks if a > time.time() - 0.1] or [0])
    assert s.proc.poll() is None and s.writes - w0 > 100 and peak > base * 3 + 5, "no audio reached the sink"
    print("music1: blocks +%d in 2 s, monitor rms %.0f (floor %.0f)" % (s.writes - w0, peak, base))
    cap.terminate()


def _check_native(s):
    """In-process stream: callback must keep pulling blocks and the mixer must produce sound."""
    peaks, orig = [], s._mix
    s._mix = lambda: (b := orig(), peaks.append(any(b)))[0]
    w0 = s.writes
    for name, pitch in SFX:
        s.handle({"kind": "sound", "name": name, "pitch": pitch, "volume": 1})
        time.sleep(0.5)
    m0 = time.time()
    while time.time() - m0 < 2:
        s.handle(MOD)
        time.sleep(1 / 60)
    assert s.stream.active and s.writes - w0 > 100 and any(peaks), "native stream not pulling sound"
    print("native: blocks +%d, latency %.1f ms" % (s.writes - w0, s.stream.latency * 1000))


def _selfcheck():
    logging.basicConfig(level=logging.INFO)
    s = Sound(probe=True)
    t = time.time()
    while (not s.ready() or not s.backend) and s.enabled and time.time() - t < 60:
        time.sleep(0.1)
    assert s.enabled and s.backend, "no player"
    print("backend=%s; decoded %d/%d files" % (s.backend, len(s.pcm), len(s.codes)))
    {"windows": _check_windows, "native": _check_native}.get(s.backend, _check_pulse)(s)
    s.close()
    print("OK")


if __name__ == "__main__":
    _selfcheck()
