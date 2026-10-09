# Balatro for Tern (window half)
Link it where the Tern window runs: `tern plugin link <repo>/plugin` (Windows: `tern.exe plugin link <repo>\plugin`; Windows Tern + game in WSL: `tern.exe plugin link \\wsl.localhost\<distro>\<repo>\plugin`).
Palette: "Balatro: New Run" / "Balatro: Continue" open a pane beside the focused one (a new tab when no pane has the focus) running `uv run --project <repo> balatro-tern [--continue]` (`<repo>` = the plugin's parent directory; double-quoted, so paths with spaces work in cmd, PowerShell and sh). A `\\wsl…` plugin path is mapped back to the WSL path and runs `<repo>/bin/balatro-tern` in the pane's WSL shell.
Status line shows ante and money from panes titled `Balatro: …`.
Keep it linked, not installed: the repo is found relative to the plugin. Run `balatro-tern setup` once first (see the main README).
