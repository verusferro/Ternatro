"""Windows-side audio sink (runs under the Windows venv python.exe): stdin s16le/44100/stereo -> WASAPI.
Started by sound.py through WSL interop. Queues whatever sound.py feeds (~80 ms ahead) and reports `pos <bytes consumed>` every 20 ms so it can pace on this clock; drops the oldest data if the pipe bursts.
`--probe`: writes "onset <ms>" to stderr = stdin read -> DAC time of the first non-silent block after silence."""
import os, sys, threading, time
import sounddevice as sd

RATE, FRAME, LEAD = 44100, 4, 0.12  # LEAD: most we keep queued; sound.py feeds ~80 ms ahead


def emit(line):
    """One os.write per line: the audio callback and the reporter thread both write stderr and must not interleave."""
    os.write(2, (line + "\n").encode())


def open_stream(callback, latency="low"):
    """Low-latency s16/44100/stereo output; WASAPI with auto_convert on Windows (shared by sound.py's native backend)."""
    kw = {}
    if sys.platform == "win32":
        wasapi = next(h for h in sd.query_hostapis() if "WASAPI" in h["name"])
        kw = dict(device=wasapi["default_output_device"], extra_settings=sd.WasapiSettings(auto_convert=True))
    return sd.RawOutputStream(samplerate=RATE, channels=2, dtype="int16", latency=latency, blocksize=0, callback=callback, **kw)
CAP = int(RATE * LEAD) * FRAME
buf, lock = bytearray(), threading.Lock()
probe = "--probe" in sys.argv
marks = []                # (stream byte position, read time) of silence->sound edges
total_in = total_out = 0  # bytes ever received / consumed-or-dropped
starved = flagged = 0     # callbacks the pipe couldn't fill / that PortAudio flagged (underflow)


def reader():
    global total_in, total_out
    stdin, quiet = sys.stdin.buffer, True
    while True:
        b = stdin.read1(4096)
        if not b:
            os._exit(0)
        now = time.perf_counter()
        with lock:
            if probe and quiet and any(b):
                marks.append((total_in, now))
            quiet = not any(b)
            total_in += len(b)
            buf.extend(b)
            if len(buf) > 2 * CAP:  # burst: keep the newest CAP bytes
                drop = len(buf) - CAP
                del buf[:drop]
                total_out += drop


def cb(out, frames, t, status):
    global total_out, starved, flagged
    n = frames * FRAME
    if status:
        flagged += 1
    with lock:
        chunk = bytes(buf[:n])
        del buf[:n]
        if marks and marks[0][0] < total_out + len(chunk):
            pos, rt = marks.pop(0)
            off = max(0, pos - total_out) / FRAME / RATE
            aud = time.perf_counter() + (t.outputBufferDacTime - t.currentTime) + off
            emit("onset %.1f" % ((aud - rt) * 1000))
        total_out += len(chunk)
    if len(chunk) < n:
        starved += 1
    out[: len(chunk)] = chunk
    out[len(chunk): n] = bytes(n - len(chunk))


def main():
    st = open_stream(cb)
    emit("ready lat=%.1fms dev=%s" % (st.latency * 1000, sd.query_devices(st.device)["name"]))
    with st:
        threading.Thread(target=reader, daemon=True).start()
        last, n = (0, 0), 0
        while True:
            time.sleep(0.02)
            # consumption feedback: the WSL clock runs ~3% fast vs Windows', so sound.py paces on this, not on its own clock
            emit("pos %d" % total_out)
            n += 1
            if n % 50 == 0 and (starved, flagged) != last:  # cumulative, printed off the audio thread
                last = (starved, flagged)
                emit("stats starved=%d flagged=%d" % last)


if __name__ == "__main__":
    main()
