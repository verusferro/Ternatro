# Ternatro

Balatro on Tern.

Play Balatro in a [Tern](https://docs.stencil.so/tern) pane. The game's own Lua code runs unmodified from **your own copy** of `Balatro.exe`. Tern draws the game natively: the game's layout, font, card motion, particles, shaders and sound. No game files are included in this repo.

## Requirements

- [Tern](https://docs.stencil.so/tern) desktop 0.6.2 or later (Windows, macOS or Linux).
- [uv](https://docs.astral.sh/uv/). It fetches Python 3.12 and the dependencies itself.
- Your own copy of Balatro (1.0.1o tested). Only `Balatro.exe` is needed (on macOS: `Balatro.app/Contents/Resources/Balatro.love`). There is no Steam auto-detect: give `setup` the path.
- Linux sound needs PortAudio (`libportaudio2`). Windows and macOS need nothing extra. Windows (native, uv + Python 3.12 from uv, WASAPI sound, about 80 ms, in Tern) is tested; on macOS a contributor has played it with the LuaJIT lupa below. The palette commands on native Windows are untested; `uv run` in a pane is tested. On Windows, a freshly installed `uv` is on `PATH` only for terminals (and a Tern window) started afterwards: restart Tern before using the plugin.
- macOS also needs a `lupa` built with LuaJIT and the Xcode command line tools (`xcode-select --install`): see [macOS: lupa with LuaJIT](#macos-lupa-with-luajit).

## Install

The same on Windows, macOS and Linux (run in any terminal; `<repo>` is wherever you cloned this):

```sh
git clone https://github.com/verusferro/Ternatro <repo>
uv run --project <repo> balatro-tern setup /path/to/Balatro.exe   # default: <repo>/Balatro/Balatro.exe
tern plugin link <repo>/plugin                         # Windows: tern.exe plugin link <repo>\plugin
```

`setup` unpacks the game's code and art from `Balatro.exe` into `<repo>/extracted`. The game refuses to start until you've run it. The plugin adds **Balatro: New Run** and **Balatro: Continue** to the palette, and the ante and money to the status line (the pane title is `Balatro: Ante n · $x`). If the commands don't show up, run **Reload plugins**. `tern plugin list` should show `balatro … window ready`. Keep the plugin linked rather than installed: it finds the repo relative to itself.

### macOS: lupa with LuaJIT

The game needs LuaJIT (`lupa.luajit21`). lupa's macOS wheels on PyPI don't include it: lupa's `setup.py` skips LuaJIT on macOS. Until lupa ships it, build lupa yourself after `setup` (LuaJIT 2.1 builds and runs on Apple Silicon and Intel):

```sh
cd <repo>
curl -L https://files.pythonhosted.org/packages/source/l/lupa/lupa-2.8.tar.gz | tar xz
sed -i '' "/platform == 'darwin' and 'luajit'/d" lupa-2.8/setup.py   # drop the macOS LuaJIT skip
uv build --wheel --python 3.12 -o lupa-wheel lupa-2.8
uv pip install --reinstall --no-index --find-links lupa-wheel lupa==2.8
rm -rf lupa-2.8 lupa-wheel
```

Install it from `--find-links` like this, not from the `.whl` path: uv treats a package installed from a file path as a different source and puts the PyPI lupa back on the next `uv run`. The version must match `uv.lock` (2.8 now); after an update that changes lupa's version, do this again with the new version.

### Optional: Windows Tern with the game in WSL

Tern runs on Windows, the game in WSL2 (Python 3.12, uv there; `ffmpeg` only for the pulse sound fallback). Do the steps above inside WSL (copy `Balatro.exe` from `/mnt/c/...` if you like), then link the plugin from PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\Tern\tern.exe" plugin link '\\wsl.localhost\Ubuntu-24.04\home\<you>\balatro\plugin'
```

Panes then run `<repo>/bin/balatro-tern` in WSL. WSLg sound has about 340 ms of delay; for about 90 ms give the game a small Windows Python environment (found automatically):

```powershell
py -3.12 -m venv "$env:LOCALAPPDATA\balatro-tern\venv"
& "$env:LOCALAPPDATA\balatro-tern\venv\Scripts\pip.exe" install numpy sounddevice
```

## Play

- Palette: **Balatro: New Run** or **Balatro: Continue** (opens beside the focused pane, in the current tab).
- Or in any Tern pane: `uv run --project <repo> balatro-tern [--seed SEED] [--continue] [--speed N]` (Linux/macOS/WSL: `<repo>/bin/balatro-tern` works too; Windows: `<repo>\bin\balatro-tern.cmd`).

The first start takes a few seconds longer while sounds are decoded and the shaders are baked. Both are cached in the data folder.

## Controls

Mouse only: click cards and buttons, hover a card for its description. The only keys: **Ctrl+C** quits (the run is saved; use Continue), and **A / D / W / S** move the card you last clicked left / right / to the first / to the last place in its row (jokers, consumables, hand).

- **Options** has the seed, **Music** (On/Off), **Sound FX** (On/Off) and **Back**. There is no New Run button there: use the palette.
- **Game over / win:** **New Run** opens the game's own New Run screen (deck, stake, challenges). There is no Main Menu.
- **Run Info** is the game's own: Poker Hands, Blinds and Vouchers.
- **Reordering:** right-click a joker, consumable or hand card for Move left / right / first / last (Tern sends no mouse motion, so there is no drag).

## Notes

- Data folder (saves, caches, `errors.log`, `prefs.json`): Windows `%LOCALAPPDATA%\balatro-tern`, macOS `~/Library/Application Support/balatro-tern`, Linux/WSL `${XDG_DATA_HOME:-~/.local/share}/balatro-tern`. The run save is `1/save.jkr` there. It is not compatible with the real game's saves. Everything is unlocked.
- The game quits on a closed pane, SIGHUP, SIGTERM, SIGINT (Windows: SIGBREAK) and when the terminal ends. Its sound process ends with it.
- Tern can't load fonts, so text is drawn as images of the game's own font.
- To test without Tern: `uv run --project <repo> python tools/bot.py --seeds 2` plays full runs headless.
- Personal use only. Balatro belongs to LocalThunk / Playstack.
