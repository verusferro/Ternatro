-- The plugin lives at <repo>/plugin. Launch is `uv run --project <repo> balatro-tern` on every OS.
-- Exception: a Windows Tern window linked over \\wsl.localhost\<distro>\... (shown as \\?\UNC\wsl.localhost\...,
-- or \\wsl$\...) has a WSL shell, which sees that path as /... and runs <repo>/bin/balatro-tern.
-- cx.layout:new_tab takes a command *line* for the tab's login shell (no argv), so quote with double
-- quotes: valid in cmd, PowerShell and sh for paths without $ or " (ponytail: no escaping beyond that).
local dir = string.gsub(tern.plugin.dir, "^\\\\%?\\", "")
local wsl = string.match(dir, "[\\/]wsl[%.%$][^\\/]*[\\/][^\\/]+([\\/].*)$")
local base
if wsl then
	base = "'" .. string.gsub(wsl, "\\", "/") .. "/../bin/balatro-tern'"
else
	local repo = string.match(dir, "^(.*)[\\/][^\\/]+[\\/]*$") or dir
	base = 'uv run --project "' .. repo .. '" balatro-tern'
end

local function launch(cx, args)
	cx.layout:new_tab({ command = base .. args })
end

tern.command({
	id = "new",
	title = "Balatro: New Run",
	group = "Balatro",
	icon = "sparkle",
	run = function(cx) launch(cx, "") end,
})

tern.command({
	id = "continue",
	title = "Balatro: Continue",
	group = "Balatro",
	icon = "sparkle",
	run = function(cx) launch(cx, " --continue") end,
})

local PREFIX = "Balatro: "
-- title: Balatro: Ante 3 · $14
tern.chrome.status(function(pane)
	local title = pane.title
	if string.sub(title, 1, #PREFIX) ~= PREFIX then
		return nil
	end
	local segs = {}
	for part in string.gmatch(string.sub(title, #PREFIX + 1) .. " · ", "(.-) · ") do
		local seg = { text = part }
		if string.sub(part, 1, 1) == "$" then
			seg.tone = "ok"
		elseif string.sub(part, 1, 5) == "Ante " then
			seg.tone = "accent"
		end
		segs[#segs + 1] = seg
	end
	return segs
end)
