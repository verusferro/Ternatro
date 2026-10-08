-- motion.lua: compact per-frame stream of the game's own animation state (VT, juice, flames, popups).
-- Loaded by motion.py into the Game's Lua runtime after boot. BT.frame() returns ONE string, tab/newline separated:
--   T real roomx roomy roomr   room offset incl. screen shake (game units / radians)
--   S a|b|c ...                structure signature: HUD scalars the Python side re-snapshots on
--   c id area idx x y w h r scale facing hl dissolve sig   one per drawn card (VT, game units), in CardArea:draw order
--   a name x y w h             CardArea rects
--   f chips_real chips_timer mult_real mult_timer c1 c2 (chips) c1 c2 (mult)   flame handler state
--   b x y w h                  play/sort/discard UIBox
--   k x y w h scale r dissolve  blind chip sprite + HUD_blind drop offset (dy)
local fmt = string.format

local AREAS = {'deck', 'discard', 'shop_jokers', 'shop_vouchers', 'shop_booster', 'hand', 'consumeables', 'jokers', 'play', 'pack_cards'}  -- DOM/z order, last on top
local HL_LAST = {joker = true, consumeable = true, shop = true, title_2 = true}
local function rgb(c) return fmt('%02x%02x%02x', math.floor(c[1] * 255 + .5), math.floor(c[2] * 255 + .5), math.floor(c[3] * 255 + .5)) end

local function card_sig(c)
  local b = c.base
  return (c.config.center_key or '?') .. '.' .. (b and (b.suit or '') .. (b.value or '') or '') .. '.' ..
    (c.edition and c.edition.type or '') .. '.' .. (c.seal or '') .. '.' .. (c.debuff and 'd' or '') .. ((c.dissolve or 0) > 0.005 and 'x' or '') .. (c.ability and (tostring(c.ability.perma_bonus) .. tostring(c.ability.mult) .. tostring(c.ability.x_mult) .. tostring(c.ability.t_mult) .. tostring(c.ability.t_chips) .. (type(c.ability.extra) == 'number' and tostring(c.ability.extra) or '')) or '')
end

local function emit_card(o, name, a, i, c)
  local vt = c.VT
  local draw = c.states.visible
  if (name == 'deck' or name == 'discard') and draw and not (name == 'deck' and i > #a.cards - 3) and math.abs(vt.x - c.T.x) < .02
    and math.abs(vt.y - c.T.y) < .02 and math.abs(vt.r - c.T.r) < .01 then draw = false end  -- settled pile: only its top shows
  if draw then
    o[#o + 1] = fmt('c\t%d\t%s\t%d\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%s\t%d\t%.3f\t%s', c.sort_id, name, i, vt.x, vt.y,
      vt.w, vt.h, vt.r, vt.scale, c.sprite_facing == 'back' and 'b' or 'f', c.highlighted and 1 or 0,
      math.abs(c.dissolve or 0), card_sig(c))
  end
end

function BT.frame()
  local o = {}
  local G = G
  if G.STAGE ~= G.STAGES.RUN or not G.GAME or not G.hand then return '' end
  local R, T = G.ROOM, G.ROOM.T
  -- update_canvas_juice: ROOM.T = ORIG + shake*(0.015*sin(0.913t), 0.015*sin(0.952t) + jiggle + cursor terms).  The slow
  -- sines are pure functions of REAL: send the rest per frame and let CSS run the sines (motion.drift_css), so a
  -- 3 px drift is drawn smoothly instead of as resent steps.
  local shake = (G.SETTINGS.reduced_motion and 0 or 1) * (tonumber(G.SETTINGS.screenshake) or 50) / 100 * 3
  if shake < 0.05 then shake = 0 end
  local t = G.TIMERS.REAL
  o[#o + 1] = fmt('T\t%.4f\t%.5f\t%.5f\t%.5f\t%.4f', t, T.x - shake * 0.015 * math.sin(0.913 * t), T.y - shake * 0.015 * math.sin(0.952 * t), T.r or 0, shake)
  local cur = G.GAME.current_round.current_hand
  o[#o + 1] = 'S\t' .. table.concat({tostring(G.STATE), tostring(G.GAME.dollars), tostring(G.GAME.round_resets.ante),
    tostring(G.GAME.round), tostring(G.GAME.current_round.hands_left), tostring(G.GAME.current_round.discards_left),
    tostring(G.GAME.chips), tostring(cur.handname_text), tostring(cur.chip_text), tostring(cur.mult_text), tostring(cur.hand_level),
    tostring(G.GAME.blind and G.GAME.blind.config.blind.key), tostring((G.GAME.STOP_USE or 0) > 0), tostring(G.deck and #G.deck.cards)}, '|')
  for _, name in ipairs(AREAS) do
    local a = G[name]
    if a and not a.REMOVED and a.cards then
      local at = a.T
      o[#o + 1] = fmt('a\t%s\t%.4f\t%.4f\t%.4f\t%.4f', name, at.x, at.y, at.w, at.h)
      -- CardArea:draw: joker / consumeable / shop rows draw their highlighted cards last, so a selected card and the
      -- USE / SELL / BUY buttons that peek out from behind it sit above its neighbours (and take their clicks)
      if HL_LAST[a.config.type] then
        for i, c in ipairs(a.cards) do if not c.highlighted then emit_card(o, name, a, i, c) end end
        for i, c in ipairs(a.cards) do if c.highlighted then emit_card(o, name, a, i, c) end end
      else
        for i, c in ipairs(a.cards) do emit_card(o, name, a, i, c) end
      end
    end
  end
  local named_areas = {}
  for _, name in ipairs(AREAS) do if G[name] then named_areas[G[name]] = true end end
  for _, c in pairs(G.I.CARD) do  -- Jimbo (win screen) and any other card that lives outside a CardArea
    if not c.REMOVED and c.states.visible and (c.jimbo or (G.OVERLAY_MENU and c.area and not named_areas[c.area])) then
      local vt = c.VT
      o[#o + 1] = fmt('c\t%d\tmisc\t1\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\tf\t0\t%.3f\t%s', c.sort_id, vt.x, vt.y,
        vt.w, vt.h, vt.r, vt.scale, math.abs(c.dissolve or 0), card_sig(c))
    end
  end
  local cf, mf = G.ARGS.chip_flames, G.ARGS.mult_flames
  if cf and mf then
    o[#o + 1] = fmt('f\t%.3f\t%.3f\t%.3f\t%.3f\t%s\t%s\t%s\t%s', cf.real_intensity, cf.timer, mf.real_intensity, mf.timer,
      rgb(cf.colour_1), rgb(cf.colour_2), rgb(mf.colour_1), rgb(mf.colour_2))
  end
  local bt = G.buttons
  if bt and not bt.REMOVED then o[#o + 1] = fmt('b\t%.4f\t%.4f\t%.4f\t%.4f', bt.VT.x, bt.VT.y, bt.VT.w, bt.VT.h) end
  local hb, bl = G.HUD_blind, G.GAME.blind
  if hb and bl then
    o[#o + 1] = fmt('k\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f', hb.VT.y - hb.T.y, bl.VT.scale or 1, bl.VT.r or 0, bl.dissolve or 0, bl.VT.w or 0, bl.VT.h or 0)
  end
  local hud = G.HUD
  if hud then o[#o + 1] = fmt('h\t%.4f\t%.4f\t%.4f\t%.4f', hud.T.x, hud.T.y, hud.T.w, hud.T.h) end
  return table.concat(o, '\n')
end

-- ===== tooltip =====  BT.tip(id): rows of  kind \t seg \t seg ...  (seg = text \1 fg \1 bg, colours hex or empty)
local function tcol(c)
  if type(c) ~= 'table' or (c[4] or 1) < 0.05 then return '' end
  return rgb(c)
end
local function walk(n, bg, out)
  if type(n) ~= 'table' then return end
  if n.n then
    local c = n.config or {}
    if n.n == G.UIT.T then
      local s = c.text ~= nil and tostring(c.text) or (c.ref_table and c.ref_value and tostring(c.ref_table[c.ref_value])) or ''
      if s ~= '' then out[#out + 1] = s .. '\1' .. tcol(c.colour) .. '\1' .. bg end
    elseif n.n == G.UIT.O then
      local o = c.object
      if o and o.strings and o.strings[1] then out[#out + 1] = tostring(o.strings[1].string) .. '\1\1' .. bg end
    else
      local b = bg
      if c.colour and n.n ~= G.UIT.ROOT then local x = tcol(c.colour); if x ~= '' and n.n == G.UIT.C or n.n == G.UIT.R then b = x ~= '' and x or bg end end
      walk(n.nodes, b, out)
    end
  else
    for _, x in ipairs(n) do walk(x, bg, out) end
  end
end
local function row(kind, node)
  local segs = {}
  walk(node, '', segs)
  if #segs == 0 then return nil end
  return kind .. '\t' .. table.concat(segs, '\t')
end
function BT.tip(id)
  local c
  for _, x in pairs(G.I.CARD) do if x.sort_id == id and x.ability then c = x break end end
  if not c then return '' end
  local t = c:generate_UIBox_ability_table()
  local o = {}
  for _, line in ipairs(t.main or {}) do o[#o + 1] = row('m', line) end
  for _, info in ipairs(t.info or {}) do
    if info.name then o[#o + 1] = row('h', info.name) end
    for _, line in ipairs(info) do o[#o + 1] = row('i', line) end
  end
  for _, b in ipairs(t.badges or {}) do
    if type(b) == 'string' then local l = G.localization.misc.labels[b]; if l then o[#o + 1] = 'b\t' .. l .. '\1\1' end end
  end
  return table.concat(o, '\n')
end
