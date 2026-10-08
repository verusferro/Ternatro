import argparse
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "extracted"


def setup(exe, out):
    exe = Path(exe) if exe else REPO / "Balatro/Balatro.exe"
    if not exe.is_file():
        raise SystemExit("Balatro.exe not found: run `balatro-tern setup PATH_TO_Balatro.exe` (your own copy of the game).")
    out = Path(out).resolve()
    with zipfile.ZipFile(exe) as z:  # the exe is a zip behind a stub; zipfile finds the directory at the end
        for m in z.infolist():
            if not (out / m.filename).resolve().is_relative_to(out):
                raise SystemExit(f"refusing unsafe path in archive: {m.filename}")
        z.extractall(out)
        print(f"extracted {len(z.infolist())} entries from {exe} to {out}")


def main():
    p = argparse.ArgumentParser(prog="balatro-tern")
    p.add_argument("--continue", dest="cont", action="store_true", help="resume the saved run")
    p.add_argument("--seed")
    p.add_argument("--speed", type=float, default=2)
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("setup", help="extract the game files from your own Balatro.exe")
    sp.add_argument("exe", nargs="?", help="path to Balatro.exe (default: auto-detect)")
    sp.add_argument("--out", default=str(OUT), help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.cmd == "setup":
        return setup(a.exe, a.out)
    if not (OUT / "main.lua").is_file():
        raise SystemExit("Game files missing: run `balatro-tern setup [PATH_TO_Balatro.exe]` first.")
    from .app import main as run
    run(a.cont, a.seed, a.speed)


if __name__ == "__main__":
    main()
