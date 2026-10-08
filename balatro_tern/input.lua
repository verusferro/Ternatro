-- input.lua: feed the game's own G.CONTROLLER.  Loaded by input.py into the Game's Lua runtime after boot.
-- Mouse only = love.mousemoved/pressed/released at a node's own cursor position (Node:put_focused_cursor).
IN = {mx = 0, my = 0}

-- the stubbed love.mouse.getPosition (stubs.lua) returns 0,0; Controller:set_cursor_position reads it every frame in mouse mode
love.mouse.getPosition = function() return IN.mx, IN.my end

-- update_canvas_juice eases the ROOM toward the cursor.  In the game the cursor follows the real mouse; here it only
-- jumps to clicked nodes (Tern reports no mouse motion), so the table would lurch toward every click and stay.
-- Feed it a centred cursor instead; the click itself still uses the real cursor position.
local juice = update_canvas_juice
function update_canvas_juice(dt)
  local c = G.CURSOR and G.CURSOR.T
  if not c then return juice(dt) end
  local x, y = c.x, c.y
  c.x, c.y = G.ROOM.T.w / 2, G.ROOM.T.h / 2
  juice(dt)
  c.x, c.y = x, y
end

function IN.find(kind, id)
  if kind == 'card' then
    for _, c in pairs(G.I.CARD) do if c.sort_id == id and not c.REMOVED then return c end end
  elseif kind == 'btn' then  -- the live UIElement whose config.button is `name` (optionally 'name#sort_id' of the card in its ref_table)
    local name, cid = tostring(id):match('^([^#]*)#?(.*)$')
    for _, n in pairs(G.MOVEABLES) do
      local c = n.config
      if c and c.button == name and not n.REMOVED and n.states.visible and n.states.click.can and (cid == '' or (c.ref_table and c.ref_table.sort_id == tonumber(cid))) then return n end
    end
  else
    for _, n in pairs(G.MOVEABLES) do if n.ID == id and not n.REMOVED then return n end end
  end
end

-- point the (virtual) mouse at a node's centre; returns false when the node is gone
function IN.aim(kind, id)
  local n = IN.find(kind, id)
  if not n then return false end
  IN.mx, IN.my = n:put_focused_cursor()
  love.mousemoved(IN.mx, IN.my, 0, 0, false)
  return true
end
function IN.down() love.mousepressed(IN.mx, IN.my, 1, false) end
function IN.up() love.mousereleased(IN.mx, IN.my, 1) end
-- after the release: park the virtual mouse off the table, or whatever slides under the last click point (the first
-- shop card after Next Round) gets the game's hover popup with no real mouse over it
function IN.away()
  IN.mx, IN.my = -10000, -10000
  love.mousemoved(IN.mx, IN.my, 0, 0, false)
end

-- Reorder a card in its row, what a drag-and-drop does in the game (Tern sends no pointer motion, so no drag):
-- CardArea:align_cards lays cards out by table order, so moving the card in area.cards is the dropped result.
-- how: 'left' | 'right' | 'first' | 'last'.  Same limits as a drag: drag.can, controller not locked, pinned stay put.
local MOVABLE = {jokers = true, consumeables = true, hand = true}
function IN.move(id, how)
  local c = IN.find('card', id)
  local a = c and c.area
  if not a or G.CONTROLLER.locked or not c.states.drag.can or c.pinned then return end
  local ok = false
  for name in pairs(MOVABLE) do if G[name] == a then ok = true end end
  if not ok then return end
  local t, i = a.cards, nil
  for k, v in ipairs(t) do if v == c then i = k end end
  local j = how == 'left' and i - 1 or how == 'right' and i + 1 or how == 'first' and 1 or #t
  j = math.max(1, math.min(#t, j))
  while j < i and t[j].pinned do j = j + 1 end  -- pinned cards keep the front
  if j == i then return end
  table.remove(t, i)
  table.insert(t, j, c)
  if a.set_ranks then a:set_ranks() end
  a:align_cards()
end

-- The game fills G.DRAW_HASH (everything the controller can collide with / hover / focus) only while drawing, and
-- nothing draws headless: run its own G:draw() (cheap, love.graphics is a stub) before each update.  The CRT tail
-- of G:draw errors on the stubs after the hash is complete; that one error is expected.
local draw_err
function IN.draw()
  local ok, e = pcall(G.draw, G)
  if not ok and not tostring(e):find('game.lua:%d+: attempt to perform arithmetic on a table value') and e ~= draw_err then
    draw_err = e
    return tostring(e)
  end
end
