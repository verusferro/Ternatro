# Ternatro

Balatro in a [Tern](https://docs.stencil.so/tern) pane. The game's own Lua runs unmodified from **your own** `Balatro.exe`; Tern draws it natively (layout, font, motion, particles, shaders, sound). No game files are included.

## Requirements

- [Tern](https://docs.stencil.so/tern) 0.6.2+ and [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12).
- Your copy of Balatro (1.0.1o tested): just `Balatro.exe` (macOS: `Balatro.app/Contents/Resources/Balatro.love`).
- Linux: PortAudio (`libportaudio2`). macOS: Xcode command line tools and a [LuaJIT lupa](#macos-lupa-with-luajit).
- Tested on Linux and native Windows; macOS played by a contributor. Windows: restart Tern after installing uv.

## Install

```sh
git clone https://github.com/verusferro/Ternatro <repo>
uv run --project <repo> balatro-tern setup /path/to/Balatro.exe   # unpacks into <repo>/extracted
tern plugin link <repo>/plugin                                    # Windows: tern.exe plugin link <repo>\plugin
```

Keep the plugin linked (it finds the repo relative to itself). No palette commands? Run **Reload plugins**.

### macOS: lupa with LuaJIT

PyPI's macOS lupa lacks LuaJIT. Build it after `setup`:

```sh
cd <repo>
curl -L https://files.pythonhosted.org/packages/source/l/lupa/lupa-2.8.tar.gz | tar xz
sed -i '' "/platform == 'darwin' and 'luajit'/d" lupa-2.8/setup.py
uv build --wheel --python 3.12 -o lupa-wheel lupa-2.8
uv pip install --reinstall --no-index --find-links lupa-wheel lupa==2.8
rm -rf lupa-2.8 lupa-wheel
```

Use `--find-links`, not the `.whl` path (else `uv run` restores PyPI's lupa). Redo when `uv.lock` changes lupa's version.

### Optional: Windows Tern, game in WSL

Install inside WSL as above, then link from PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\Tern\tern.exe" plugin link '\\wsl.localhost\Ubuntu-24.04\home\<you>\balatro\plugin'
```

For lower sound delay (~90 ms instead of ~340 ms), add a small Windows Python env (found automatically):

```powershell
py -3.12 -m venv "$env:LOCALAPPDATA\balatro-tern\venv"
& "$env:LOCALAPPDATA\balatro-tern\venv\Scripts\pip.exe" install numpy sounddevice
```

## Play

- Palette: **Balatro: New Run** / **Balatro: Continue** (opens beside the focused pane).
- Or in a pane: `uv run --project <repo> balatro-tern [--seed SEED] [--continue] [--speed N]`.

In a narrow pane (about 40% of the screen's width or less) the HUD moves under the table.

## Controls

Mouse only. **Ctrl+C** quits (the run is saved). **A / D / W / S** move the last clicked card left / right / first / last; right-click does the same (Tern has no drag).

## Notes

- Data folder (saves, caches, `errors.log`): `%LOCALAPPDATA%\balatro-tern`, `~/Library/Application Support/balatro-tern` or `~/.local/share/balatro-tern`. Saves don't mix with the real game's. Everything is unlocked.
- The first start bakes all 35 backgrounds in the background at idle priority (about 3 min on 12 cores, about 600 MB in the data folder). Until a background is ready, a plain one shows.
- Headless test: `uv run --project <repo> python tools/bot.py --seeds 2`.
- Personal use only. Balatro belongs to LocalThunk / Playstack.
