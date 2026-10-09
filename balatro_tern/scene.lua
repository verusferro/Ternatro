-- scene.lua: walks every live UIBox (HUD, blind select, shop, cash out, overlays, tooltips ...) and packs what the
-- game's own UI engine drew into two parallel strings; scene.py turns them into TSP nodes + a per-box stylesheet.
--   SC.frame() -> S, F      S = structure (changes => rebuild nodes), F = per-frame geometry/colours, line i of F is line i of S
--   S lines:  B \t boxid \t layer \t card                a UIBox (layer m|u|a|f|c|t|o|p, bottom -> top draw order;
--                                                       card = the sort_id of the card an 'f' box belongs to)
--             e \t id \t kind \t bid \t text \t shadow \t vert \t obj      one drawn UIElement (kind R C B T O)
--   F lines:  B \t boxid                                  (same row as its S line)
--             id \t vis \t x \t y \t w \t h \t scale \t r \t fill \t emb \t embcol \t shx \t shy \t shcol \t outw \t outcol \t radius \t extra
-- Units are game units (UIE.VT, ROOM-relative; add ROOM.T like the cards).  Colours are rrggbb or rrggbbaa, '' = none.
-- Python gets a click target as the UIElement's Node.ID (`SC.uie(id)`), identical to Input's ('uie', ID) refs.
SC = SC or {}
local fmt, floor, min, max = string.format, math.floor, math.min, math.max

local function clamp01(v) return v < 0 and 0 or v > 1 and 1 or v end
local function hx(c, a)
  if type(c) ~= 'table' then return '' end
  a = a or c[4] or 1
  if a <= 0.004 then return '' end
  local s = fmt('%02x%02x%02x', floor(clamp01(c[1] or 0) * 255 + .5), floor(clamp01(c[2] or 0) * 255 + .5), floor(clamp01(c[3] or 0) * 255 + .5))
  if a < 0.996 then s = s .. fmt('%02x', floor(clamp01(a) * 255 + .5)) end
  return s
end
local function f3(v) return fmt('%.3f', v or 0) end
local function clean(s) return (tostring(s):gsub('[\t\n\r\2]', ' ')) end

local UIT_T, UIT_B, UIT_C, UIT_R, UIT_O, UIT_ROOT
local function init_uit()
  if UIT_T then return end
  UIT_T, UIT_B, UIT_C, UIT_R, UIT_O, UIT_ROOT = G.UIT.T, G.UIT.B, G.UIT.C, G.UIT.R, G.UIT.O, G.UIT.ROOT
end

-- ===== atlases (name -> file, cell w, cell h, frames) for the PNG/GIF helper in Python =====
function SC.atlases()
  local o = {}
  for _, list in ipairs({G.asset_atli or {}, G.animation_atli or {}}) do
    for _, a in ipairs(list) do o[#o + 1] = table.concat({a.name, a.path, a.px, a.py, a.frames or 1}, '\t') end
  end
  return table.concat(o, '\n')
end

-- ===== objects inside UIT.O =====
local function sprite_of(obj)
  if obj.atlas and obj.sprite_pos or obj.animation then return obj end
  local c = obj.children
  if c and c.animatedSprite then return c.animatedSprite end
  if obj.tag_sprite then return obj.tag_sprite end
end

local function letter_colour(obj, k, l)
  local c = l.prefix or l.suffix or l.colour or obj.colours[k % #obj.colours + 1]
  return hx(c)
end

-- returns S descriptor, F extra for the object (or nil to draw nothing)
-- an object's rect; inside a tip (a still, built this frame) the object's own VT still lags while the host UI slides in, so take its UIElement's
local function obj_vt(e, obj)
  if not SC.in_tip then return obj.VT end
  return setmetatable({x = e.VT.x, y = e.VT.y}, {__index = obj.VT})
end

local function object_info(e, obj)
  if obj.is == nil then return nil end
  if SC.in_tip and obj:is(CardArea) then SC.tip_areas[#SC.tip_areas + 1] = {obj, e} end  -- drawn into the tip (build_tip)
  if obj:is(Card) or obj:is(CardArea) or obj:is(Particles) then return nil end
  if obj:is(DynaText) then
    local st = obj.strings[obj.focused_string]
    if not st then return nil end
    -- per letter, in em of the text (1em = obj.scale game units = the font's 200px line): width (dims.x incl.
    -- spacing), offset x/y (quiver, pop-in), rotation, draw scale (pop_in * letter.scale), shadow scale (pop_in)
    local k = obj.font.FONTSCALE / G.TILESIZE
    local sc = obj.scale
    local letters, per = st.letters, {}
    -- float / bump (text.lua update_text) are pure functions of G.TIMERS.REAL with phase 200*i per letter: send their
    -- rate and amplitude once and let Tern run them (scene.py keyframes) instead of a new offset every frame.
    -- Only while nothing else (a quivering text, a running pulse) feeds the offset, scale or tilt.  The game stops a
    -- quiver with set_quiver(0) and leaves the table set, and text.lua never clears config.pulse once it has run (a
    -- pulse shows as scaled letters): the flags alone would stream the HUD's floating chips / mult digits every
    -- frame for the rest of the run.
    local scaled = false
    for _, l in ipairs(letters) do if math.abs((l.scale or 1) - 1) > 1e-3 then scaled = true; break end end
    local quiver = obj.config.quiver and (obj.config.quiver.amount or 0) ~= 0
    local mode, rate, amp, wob = '', 0, 0, ''
    if not G.SETTINGS.reduced_motion and not quiver and not scaled then
      if obj.config.bump then
        mode, rate, amp = 'b', obj.bump_rate, obj.bump_amount * math.sqrt(sc) * 7
      elseif obj.config.float then
        mode, rate, amp = 'f', 2.666, math.sqrt(sc) * k * 2000
      end
      -- rotate: letter.r = static tilt + 0.02*sin(2*REAL + i), the sine likewise left to Tern
      if obj.config.rotate then wob = obj.config.rotate == 2 and '-1' or '1' end
    end
    local n = #letters
    for i, l in ipairs(letters) do
      local p = (SC.in_tip or obj.config.min_cycle_time == 0) and 1 or (l.pop_in or 1)
      local off = l.offset or {x = 0, y = 0}
      local oy = mode == 'b' and 0 or mode == 'f' and math.sqrt(sc) * 2 or off.y
      local r = wob ~= '' and tonumber(wob) * 0.2 * (-n / 2 - 0.5 + i) / n or (l.r or 0)
      per[i] = fmt('%.4f,%.4f,%.4f,%.3f,%.3f,%.3f', l.dims.x * k / sc, -0.5 * off.x * k / sc, -0.5 * oy * k / sc,
        r, p * (l.scale or 1), p)
    end
    -- every string is in the structure (the node tree stays when a cycling DynaText changes `focused`); the letters'
    -- colours go with the frame (F field 18), since some animate (G.C.EDITION, DARK_EDITION) and must not rebuild the tree
    local texts, colss = {}, {}
    for j, s in ipairs(obj.strings) do
      local cs, same = {}, true
      for i, l in ipairs(s.letters) do
        cs[i] = letter_colour(obj, i, l)
        if cs[i] ~= cs[1] then same = false end
      end
      if same then cs = {cs[1] or ''} end
      texts[j], colss[j] = clean(s.string), table.concat(cs, ',')
    end
    local sh = obj.shadow and (obj.config.shadow_colour and hx(obj.config.shadow_colour) or hx({0, 0, 0, 0.3 * (obj.colours[1][4] or 1)})) or ''
    -- string origin inside the object's box: W_offset/H_offset + TEXT_OFFSET + spacing (text.lua draw)
    local ox = st.W_offset + obj.text_offset.x * k + (obj.config.spacing or 0) * k
    local oy = st.H_offset + obj.text_offset.y * k
    -- main letters shift by the unit shadow direction (_shadow_norm), the shadow copy by -shadow_parrallax*scale
    local px, py = obj.shadow_parrallax.x, obj.shadow_parrallax.y
    local pn = math.sqrt(px * px + py * py)
    local nx, ny = pn > 0 and px / pn * k or 0, pn > 0 and py / pn * k or 0
    local vt = obj_vt(e, obj)
    local desc = table.concat({'D', table.concat(texts, '\5'), '', sh, f3(sc), '0'}, '\3')
    return desc, table.concat({f3(vt.x), f3(vt.y), f3(vt.w), f3(vt.h), f3(vt.scale), f3(vt.r), f3(ox), f3(oy), table.concat(per, ';'),
      f3(-px * sc / G.TILESIZE), f3(-py * sc / G.TILESIZE), fmt('%.4f', nx), fmt('%.4f', ny), mode, fmt('%.4f', rate), fmt('%.4f', -0.5 * amp * k / sc), wob,
      obj.focused_string - 1, table.concat(colss, '\5')}, '\3')
  end
  if e.config.id == 'flame_chips' or e.config.id == 'flame_mult' then
    -- flame_handler: a 2.5 x 2.5 Sprite aligned 'bmi' (bottom-middle inside) to the chips / mult box, drawn with flame.fs
    -- at G.ARGS.*_flames.real_intensity.  Placed from the box itself, so it never lags behind the HUD.
    local p, s = e.parent.VT, obj.T
    local fl = G.ARGS[e.config.id == 'flame_chips' and 'chip_flames' or 'mult_flames']
    return 'F\3' .. (e.config.id == 'flame_chips' and 'c' or 'm'), table.concat({f3(p.x + (p.w - s.w) / 2), f3(p.y + p.h - s.h), f3(s.w), f3(s.h),
      '1', '0', '0', '0', '', (fl and fl.real_intensity >= 0.1) and '1' or '0'}, '\3')
  end
  if obj:is(UIBox) then return nil end
  local sp = sprite_of(obj)
  if sp and sp.atlas then
    local at = sp.atlas
    local frames = (obj.animation or sp.animation) and (at.frames or 1) or 1
    local px, py
    if sp.animation and sp.animation.y then px, py = sp.animation.x or 0, sp.animation.y
    else px, py = sp.sprite_pos and sp.sprite_pos.x or 0, sp.sprite_pos and sp.sprite_pos.y or 0 end
    local vt = obj.VT
    local dis = obj.dissolve and obj.dissolve > 0.005 and obj.dissolve or 0
    local desc = table.concat({'S', at.name, px, py, frames}, '\3')
    return desc, table.concat({f3(vt.x), f3(vt.y), f3(vt.w), f3(vt.h), f3(vt.scale), f3(vt.r), f3(dis), f3(G.ANIMATION_FPS or 10), ''}, '\3')
  end
  return nil
end

-- ===== one UIElement =====
local function emit(e, S, Fo, vis_parent, ctrl)
  local c = e.config
  local vis = vis_parent and e.states.visible
  local kind = e.UIT
  local vt = e.VT
  if kind == UIT_T then
    local clr = c.colour
    if not clr or (clr[4] or 1) <= 0.004 then return vis end
    if c.button_UIE and not c.button_UIE.config.button then clr = G.C.UI.TEXT_INACTIVE end
    local scale = c.scale or 1
    local txt = c.text
    if txt == nil then return vis end
    local sh = (c.shadow or (c.button_UIE and c.button_UIE.config.button)) and G.SETTINGS.GRAPHICS.shadows == 'On'
    local sp = e.shadow_parrallax
    local btn = (c.button_UIE and c.button_UIE.config.button and c.button_UIE) or (c.button and e) or nil
    S[#S + 1] = table.concat({'e', e.ID, 'T', btn and btn.ID or 0, clean(txt), sh and '1' or '0', c.vert and '1' or '0', '', (c.on_demand_tooltip or c.tooltip or c.detailed_tooltip) and 'u' or ''}, '\t')
    Fo[#Fo + 1] = table.concat({e.ID, vis and '1' or '0', f3(vt.x), f3(vt.y), f3(vt.w), f3(vt.h), f3(vt.scale), f3(vt.r), hx(clr), '0', '', f3(-sp.x * 0.5 / G.TILESIZE), f3(-sp.y * 0.5 / G.TILESIZE), '', '0', '', f3(scale), ''}, '\t')
    return vis
  end
  local btn = (c.button_UIE and c.button_UIE.config.button and c.button_UIE) or (c.button and e) or nil
  local obj_desc, obj_f
  if kind == UIT_O and c.object then
    obj_desc, obj_f = object_info(e, c.object)
    if not obj_desc then
      if c.object.is and c.object:is(UIBox) and c.object.states.visible and vis then ctrl.nested[#ctrl.nested + 1] = c.object end
      return vis
    end
    -- Sprite:draw skips a hidden sprite: create_toggle keeps its check mark in the tree, hidden while the toggle is off
    if c.object.is and c.object:is(Sprite) and not c.object.states.visible then vis = false end
  end
  local fill, hover, emb, embcol, shx, shy, shcol, outw, outcol, rad = '', 0, 0, '', 0, 0, '', 0, '', 0
  if kind ~= UIT_O then
    local clr = c.colour
    fill = hx(clr)
    -- hover from the game's own state would follow the virtual cursor parked on the last click (Tern sends no mouse
    -- motion), so only the click flash comes from the game; mouse hover is CSS (scene.py `.scb:hover`)
    local hv = btn and btn.last_clicked and btn.last_clicked > G.TIMERS.REAL - 0.1
    if fill ~= '' and c.button_delay then fill = hx(mix_colours(clr, G.C.L_BLACK, 0.5)) end
    if fill ~= '' and hv then
      local h = G.C.UI.HOVER
      local a = h[4]
      local m = {clr[1] * (1 - a) + h[1] * a, clr[2] * (1 - a) + h[2] * a, clr[3] * (1 - a) + h[3] * a, clr[4]}
      fill = hx(m)
    end
    if clr and (clr[4] or 1) > 0.01 then
      local pd = 1.5
      if c.button and ((e.last_clicked and e.last_clicked > G.TIMERS.REAL - 0.1) or (e.states.hover.is and G.CONTROLLER.is_cursor_down)) then pd = 0 end
      local sp = e.shadow_parrallax
      if c.emboss then
        emb = c.emboss
        embcol = hx({darken(clr, 0.3, true)})
      end
      if c.shadow and G.SETTINGS.GRAPHICS.shadows == 'On' then
        shx, shy = -sp.x * pd * 0.98 / G.TILESIZE, -sp.y * pd * 0.98 / G.TILESIZE
        shcol = c.shadow_colour and hx(c.shadow_colour) or hx({0, 0, 0}, 0.3 * (clr[4] or 1))
      end
    end
    if c.outline and c.outline_colour and (c.outline_colour[4] or 1) > 0.01 then outw, outcol = c.outline / G.TILESIZE, hx(c.outline_colour) end
    if c.r and vt.w > 0.01 then
      local m = min(vt.w, vt.h)
      local res = c.res or (m > 3.5 and 0.8 or m > 0.3 and 0.6 or 0.15)
      rad = 4 * res / G.TILESIZE
    end
  end
  local prog = ''
  if c.progress_bar then
    local pb = c.progress_bar
    prog = fmt('%.3f/%s/%s', clamp01((pb.ref_table[pb.ref_value] or 0) / (pb.max or 1)), hx(pb.filled_col or G.C.BLUE), hx(pb.empty_col or G.C.GREY))
  end
  if fill == '' and outcol == '' and shcol == '' and embcol == '' and not obj_desc and not btn and prog == '' then return vis end
  -- a tip is built (and clamped into the room by the game) from the element's pose now: wait until its host UI stopped sliding in
  local settled = math.abs(vt.x - e.T.x) + math.abs(vt.y - e.T.y) <= 0.05
  local has_tip = settled and (c.on_demand_tooltip or c.tooltip or c.detailed_tooltip or c.func == 'hover_tag_proxy' or (kind == UIT_O and c.object and c.object.config and c.object.config.tag)) and 'u' or ''
  S[#S + 1] = table.concat({'e', e.ID, kind == UIT_O and 'O' or 'R', btn and btn.ID or 0, '', '0', '0', obj_desc or '', has_tip}, '\t')
  local extra = obj_f or ''
  Fo[#Fo + 1] = table.concat({e.ID, vis and '1' or '0', f3(vt.x), f3(vt.y), f3(vt.w), f3(vt.h), f3(vt.scale), f3(vt.r), fill, f3(emb), embcol,
    f3(shx), f3(shy), shcol, f3(outw), outcol, f3(rad), table.concat({c.chosen and '1' or '0', prog, extra}, '\4')}, '\t')
  return vis
end

-- ===== particles (engine/particles.lua): rotated squares at offset, `scale` wide, colour * (1 - fade_alpha) =====
-- One element per emitter with a pool of `n` slots (high-water mark, so births/deaths don't rebuild nodes).
-- (Placed per frame: as Tern CSS animations each particle would cost Tern a compositing pass every frame.)
local function is_particles(v) return getmetatable(v) == Particles end
local function emit_particles(p, S, Fo, vis)
  local live = 0
  for _, v in pairs(p.particles) do if v.draw then live = live + 1 end end
  p.sc_pool = math.max(p.sc_pool or 8, math.ceil(live / 8) * 8)
  S[#S + 1] = table.concat({'e', p.ID, 'P', 0, '', '0', '0', 'P\3' .. p.sc_pool, ''}, '\t')
  local vt = p.VT
  local cx, cy = vt.x + p.T.w / 2, vt.y + p.T.h / 2
  local a = 1 - (p.fade_alpha or 0)
  local parts = {}
  for _, v in pairs(p.particles) do
    if v.draw and #parts < p.sc_pool then
      local c = hx(v.colour, (v.colour[4] or 1) * a)
      if c ~= '' then parts[#parts + 1] = fmt('%.3f,%.3f,%.3f,%.3f,%s', cx + v.offset.x, cy + v.offset.y, v.scale, v.facing, c) end
    end
  end
  Fo[#Fo + 1] = table.concat({p.ID, vis and '1' or '0', f3(vt.x), f3(vt.y), f3(vt.w), f3(vt.h), '1', '0', '', '0', '', '0', '0', '', '0', '', '0',
    table.concat(parts, ';')}, '\t')
end

local function walk(e, S, Fo, vis_parent, ctrl)
  local vis = emit(e, S, Fo, vis_parent, ctrl)
  if not e.children then return end
  -- drawn in `pairs` order = insertion order for the array part; draw_layer children are drawn by the box afterwards
  for _, ch in ipairs(e.children) do
    if is_particles(ch) then emit_particles(ch, S, Fo, vis)
    elseif not ch.config.draw_layer then walk(ch, S, Fo, vis, ctrl) end
  end
end

-- Game:draw order: parentless Moveables (pack sparkles) < UIBoxes < card areas/cards (+ their boxes and particles)
-- < attention_text boxes < overlay menu < popups.  A card's own boxes ('f') also return the card: they are drawn
-- with it (Card:draw), right under its face, so Python places them in the DOM just before that card.
local function layer_of(b)
  if b == G.OVERLAY_MENU then return 'o' end
  if b.attention_text then return 't' end
  local p = b.config and b.config.parent
  -- Card:draw draws its focus frame (card_focus_ui) before the card, except in the hand, and its price tag and
  -- buy / use buttons before its sprite too (they peek out from behind the card)
  if p and p.children and p.children.focused_ui == b and p.area ~= G.hand then return 'f', p.sort_id end
  local ch = p and p.children
  if ch and (ch.price == b or ch.buy_button == b or ch.buy_and_use_button == b or ch.use_button == b) then return 'f', p.sort_id end
  if p then
    if p.is and p:is(CardArea) then return 'a' end
    return 'c'
  end
  return 'u'
end

local SKIP = {'debug_tools', 'online_leaderboard', 'achievement_notification', 'screenwipe', 'OVERLAY_TUTORIAL', 'SPLASH_BACK', 'SPLASH_FRONT'}
-- CardArea:draw draws its area_uibox (backing + n/limit) only in some states: the hand's is gone in shop / packs /
-- round eval / blind select, and the deck view hides deck, hand and play
local HAND_HIDDEN
local function area_box_hidden(b)
  local a = b.config and b.config.parent
  if not (a and a.children and a.children.area_uibox == b) then return false end
  if not a.states.visible then return true end
  if G.VIEWING_DECK and (a == G.deck or a == G.hand or a == G.play) then return true end
  local S = G.STATES
  HAND_HIDDEN = HAND_HIDDEN or {[S.SHOP] = 1, [S.TAROT_PACK] = 1, [S.SPECTRAL_PACK] = 1, [S.STANDARD_PACK] = 1, [S.BUFFOON_PACK] = 1,
    [S.PLANET_PACK] = 1, [S.ROUND_EVAL] = 1, [S.BLIND_SELECT] = 1}
  return a.config.type == 'hand' and HAND_HIDDEN[G.TAROT_INTERRUPT or G.STATE] ~= nil
end
local function skipped(b)
  if b.REMOVED or area_box_hidden(b) then return true end
  for _, k in ipairs(SKIP) do if G[k] == b then return true end end
  return false
end

local ORDER = {m = 0, u = 1, a = 2, f = 3, c = 4, t = 5, o = 6, p = 7}

local function dump_box(b, layer, S, Fo, card)
  S[#S + 1] = table.concat({'B', b.ID, layer, card or ''}, '\t')
  Fo[#Fo + 1] = 'B\t' .. b.ID
  local ctrl = {nested = {}}
  -- UIBox:draw draws its children (attached particles) before its own UI
  for _, v in ipairs(b.children or {}) do if is_particles(v) then emit_particles(v, S, Fo, b.states.visible) end end
  if b.UIRoot then walk(b.UIRoot, S, Fo, b.states.visible, ctrl) end
  for _, v in ipairs(b.draw_layers or {}) do if v.draw_self then walk(v, S, Fo, b.states.visible, ctrl) end end
  return ctrl.nested
end

-- a particle emitter that no dumped UIBox draws: parentless (pack sparkles) or attached to a card / blind / Jimbo
local function dump_emitter(p, layer, S, Fo)
  S[#S + 1] = table.concat({'B', p.ID, layer}, '\t')
  Fo[#Fo + 1] = 'B\t' .. p.ID
  emit_particles(p, S, Fo, p.states.visible)
end

function SC.frame()
  init_uit()
  if G.STAGE ~= G.STAGES.RUN then return '', '' end
  local list, seen = {}, {}
  local function add(b, layer)
    if seen[b] or skipped(b) or not b.states.visible then return end
    seen[b] = true
    local card
    if not layer then layer, card = layer_of(b) end
    list[#list + 1] = {b, layer, nil, nil, card}
  end
  for _, b in ipairs(G.I.UIBOX) do
    -- a UIBox that is a UIT.O's object is drawn by its container (dump_box's `nested`), on its layer and above it
    local par = b.parent
    if not (par and par.config and par.config.object == b) then add(b) end
  end
  for _, b in ipairs(G.I.POPUP or {}) do
    local par = b.config and b.config.parent  -- only popups the game itself still draws: parent visible and hovered / focused
    if par and par.states and par.states.visible and (par.states.hover.is or par.states.focus.is) then add(b, 'p') end
  end
  for _, p in ipairs(G.I.MOVEABLE) do
    if is_particles(p) and (#p.particles > 0 or p.sc_pool) and not p.REMOVED then
      local par = p.parent
      if not par then list[#list + 1] = {p, 'm', 'P'}
      elseif not (par.is and (par:is(UIBox) or par:is(UIElement))) then list[#list + 1] = {p, 'c', 'P'} end
    end
  end
  -- the HUD and the boxes hung on its row_blind keep this order among themselves.  Wide: first in their layer, where
  -- start_run created them (a rebuilt HUD is the newest box and would cover HUD_blind); tall: last, see below
  local GROUP = {G.HUD, G.HUD_blind, G.blind_prompt_box, G.SHOP_SIGN}
  local function group_rank(b) for r = 1, 4 do if GROUP[r] == b then return r end end return 0 end
  -- stable sort by layer (table.sort is not stable: tag with the index)
  for i, v in ipairs(list) do
    v[4] = i
    if not SC.tall and group_rank(v[1]) > 0 then v[4] = group_rank(v[1]) - 1e6 end
  end
  table.sort(list, function(a, b) if a[2] ~= b[2] then return ORDER[a[2]] < ORDER[b[2]] end return a[4] < b[4] end)
  -- tall: the strip is dumped last (after the nested boxes too), so it covers the panels that hang below the room as the
  -- screen edge does in the original, and the boxes hung on its row_blind come after it (S order = draw order within a layer)
  local function tail_rank(b) return SC.tall and group_rank(b) or 0 end
  local S, Fo, tail = {}, {}, {}
  local i, flushed = 1, false
  while true do
    if i > #list then
      if flushed or #tail == 0 then break end
      table.sort(tail, function(a, b) local ra, rb = tail_rank(a[1]), tail_rank(b[1]) return ra < rb or (ra == rb and a[4] < b[4]) end)
      for _, v in ipairs(tail) do list[#list + 1] = v end
      flushed = true
    end
    local b, layer = list[i][1], list[i][2]
    if not flushed and tail_rank(b) > 0 then
      tail[#tail + 1] = list[i]
    elseif list[i][3] == 'P' then
      dump_emitter(b, layer, S, Fo)
    else
      local nested = dump_box(b, layer, S, Fo, list[i][5])
      for _, nb in ipairs(nested) do if not seen[nb] then seen[nb] = true; list[#list + 1] = {nb, layer, nil, #list + 1, list[i][5]} end end
    end
    i = i + 1
  end
  return table.concat(S, '\n'), table.concat(Fo, '\n')
end

-- the UIElement with Node.ID `id` (what Input aims the virtual mouse at)
function SC.uie(id)
  for _, n in pairs(G.MOVEABLES) do if n.ID == id then return n end end
end

-- ===== popups: the game's own tooltip UIBox, built hidden, dumped relative to its parent's top-left, removed =====
-- Returns S, F in the SC.frame format; coordinates are relative to `origin` (the parent's T = the pose it is aligned to).
G.I.SCENE_TIP = G.I.SCENE_TIP or {}
local function build_tip(def, cfg, origin)
  init_uit()
  local ok, S, Fo, K = pcall(function()
    cfg.instance_type = 'SCENE_TIP'
    local box = UIBox{definition = def, config = cfg}
    box.states.collide.can = false
    -- a tip is a still: while the host UI slides in (blind select, overlay) the new box's VT lags its T, which shifted the popup
    local function snap(n) n:hard_set_VT(); for _, c in pairs(n.children or {}) do if c.is and (c:is(UIElement) or c:is(UIBox)) then snap(c) end end end
    snap(box); if box.UIRoot then snap(box.UIRoot) end
    local S, Fo = {}, {}
    SC.in_tip, SC.tip_areas = true, {}  -- a tip is a still of its finished state: DynaText letters fully popped in (else the name is invisible)
    dump_box(box, 'p', S, Fo)
    SC.in_tip = false
    -- cards of CardAreas in the tip (create_UIBox_hand_tip's examples): laid out by the area on its UIElement's pose
    local K = {}
    for _, ae in ipairs(SC.tip_areas) do
      local area, el = ae[1], ae[2]
      area.T.x, area.T.y = el.T.x, el.T.y
      area:align_cards()
      for i, c in ipairs(area.cards) do
        local sc = (cfg.card_scales and cfg.card_scales[i]) or 1
        K[#K + 1] = table.concat({BT.card_json(c), f3(c.T.x), f3(c.T.y), f3(c.T.w), f3(c.T.h), fmt('%.4f', c.T.r or 0), f3(sc)}, '\t')
      end
      for i = #area.cards, 1, -1 do area.cards[i]:remove() end  -- else they'd linger in G.I.CARD (motion.lua draws stray cards)
      area:remove()
    end
    box:remove()
    return S, Fo, K
  end)
  SC.in_tip = false
  if not ok then return '', '', '' end
  local ox, oy = origin.x, origin.y
  for i, row in ipairs(Fo) do  -- rebase every F row (x,y are fields 3,4); nested object rects (D/S/F) carry their own VT in the last field
    if row:sub(1, 2) ~= 'B\t' then
      local f = {}
      for v in (row .. '\t'):gmatch('([^\t]*)\t') do f[#f + 1] = v end
      f[3] = f3(tonumber(f[3]) - ox)
      f[4] = f3(tonumber(f[4]) - oy)
      local ex = f[#f]
      local parts = {}
      for v in (ex .. '\4'):gmatch('([^\4]*)\4') do parts[#parts + 1] = v end
      local last = parts[#parts]
      if last and last:find('\3', 1, true) then
        local o = {}
        for v in (last .. '\3'):gmatch('([^\3]*)\3') do o[#o + 1] = v end
        o[1] = f3(tonumber(o[1]) - ox)
        o[2] = f3(tonumber(o[2]) - oy)
        parts[#parts] = table.concat(o, '\3')
        f[#f] = table.concat(parts, '\4')
      end
      Fo[i] = table.concat(f, '\t')
    end
  end
  local Kr = {}
  for i, row in ipairs(K or {}) do
    local f = {}
    for v in (row .. '\t'):gmatch('([^\t]*)\t') do f[#f + 1] = v end
    f[2], f[3] = f3(tonumber(f[2]) - ox), f3(tonumber(f[3]) - oy)
    Kr[i] = table.concat(f, '\t')
  end
  return table.concat(S, '\n'), table.concat(Fo, '\n'), table.concat(Kr, '\n')
end

-- card_h_popup of a Card, as Card:hover builds it
function SC.tip(id)
  local card
  for _, c in pairs(G.I.CARD) do if c.sort_id == id and c.ability then card = c break end end
  if not card or card.REMOVED then return '', '' end
  local ok, def, cfg = pcall(function()
    card.ability_UIBox_table = card:generate_UIBox_ability_table()
    local cfg = card:align_h_popup()
    cfg.parent = card
    return G.UIDEF.card_h_popup(card), cfg
  end)
  if not ok then return '', '' end
  return build_tip(def, cfg, card.T)
end

-- tooltip of a UIElement (UIElement:hover) or of a Tag sprite (tag.lua hover); kind from the S line field 9
function SC.tip_uie(id)
  local e = SC.uie(id)
  if not e or e.REMOVED then return '', '' end
  local c = e.config
  local ps = play_sound
  play_sound = function() end  -- create_UIBox_hand_tip plays 'paper1' as it builds: a prebuilt tip isn't a hover
  local ok, def, cfg, origin = pcall(function()
    if c.on_demand_tooltip then
      local low = e.T.y > G.ROOM.T.h / 2
      local cfg = {align = low and 'tm' or 'bm', offset = {x = 0, y = low and -0.1 or 0.1}, parent = e}
      local fl = c.on_demand_tooltip.filler
      local hand = fl and fl.func == create_UIBox_hand_tip and G.GAME.hands[fl.args]
      if hand and hand.example then  -- create_UIBox_hand_tip eases scoring cards to scale +0.25, the others -0.15
        cfg.card_scales = {}
        for i, v in ipairs(hand.example) do cfg.card_scales[i] = v[2] and 1.25 or 0.85 end
      end
      return create_popup_UIBox_tooltip(c.on_demand_tooltip), cfg, e.T
    elseif c.tooltip then
      return create_popup_UIBox_tooltip(c.tooltip), {align = 'tm', offset = {x = 0, y = -0.1}, parent = e}, e.T
    elseif c.detailed_tooltip then
      return create_UIBox_detailed_tooltip(c.detailed_tooltip), {align = 'tm', offset = {x = 0, y = -0.1}, parent = e}, e.T
    end
    local spr = c.object
    local tag = spr and spr.config and spr.config.tag
    if c.func == 'hover_tag_proxy' then tag = c.ref_table; spr = tag.tag_sprite end  -- the Skip Blind button
    if tag then
      tag:get_uibox_table(spr)
      -- Blind Select: the sprite itself can't collide; hover_tag_proxy pops the tag over its row (the sprite and the Skip button)
      local row = e.parent
      while row and row.config.ref_table ~= spr do row = row.parent end
      if row and not spr.states.collide.can then
        return G.UIDEF.card_h_popup(spr), {align = 'tm', offset = {x = 0, y = -0.1}, parent = row}, e.T
      end
      return G.UIDEF.card_h_popup(spr), {align = 'cl', offset = {x = -0.1, y = 0}, parent = spr}, spr.T
    end
  end)
  play_sound = ps
  if not ok or not def then return '', '' end
  return build_tip(def, cfg, origin)
end

-- debug: put the game into a state we cannot reach quickly (BALATRO_DEBUG token via scene.force)
function SC.force(what)
  if what == 'win' then
    G.GAME.won = true
    win_game()
  elseif what == 'over' then
    G.STATE = G.STATES.GAME_OVER
    G.STATE_COMPLETE = false
  elseif what == 'options' then
    G.FUNCS.options({config = {}})
  end
end

-- ===== Options menu: only what this port supports, built from the game's own pieces =====
-- The seed and the sidecar's Music / Sound FX toggles. Dropped: New Run, Settings (video / graphics), Main Menu (no
-- menu stage here), Stats, Collection, Customize Deck, Copy (no clipboard).
-- Python: scene.on_toggle(kind) is called by the buttons; scene.set_mute(music_muted, sfx_muted) keeps the labels.
SC.mute = SC.mute or {music = 'Music: On', sfx = 'Sound FX: On'}
G.FUNCS.scene_toggle_music = function(e) if SC.py_toggle then SC.py_toggle('music') end end
G.FUNCS.scene_toggle_sfx = function(e) if SC.py_toggle then SC.py_toggle('sfx') end end
local function mute_button(kind, button)
  local def = UIBox_button{label = {SC.mute[kind]}, button = button, minw = 5}
  local function find(n)
    if n.n == G.UIT.T then n.config.ref_table, n.config.ref_value = SC.mute, kind return true end
    for _, c in ipairs(n.nodes or {}) do if find(c) then return true end end
  end
  find(def)
  return def
end
function create_UIBox_options()
  local seed = {n = G.UIT.R, config = {align = "cm", padding = 0.05}, nodes = {
    {n = G.UIT.C, config = {align = "cm"}, nodes = {{n = G.UIT.T, config = {text = localize('b_seed') .. ": ", scale = 0.4, colour = G.C.WHITE}}}},
    {n = G.UIT.C, config = {align = "cm", r = 0.1, colour = G.GAME.seeded and G.C.RED or G.C.BLACK, minw = 1.8, minh = 0.5, padding = 0.1, emboss = 0.05}, nodes = {
      {n = G.UIT.T, config = {text = tostring(G.GAME.pseudorandom.seed), scale = 0.43, colour = G.C.UI.TEXT_LIGHT, shadow = true}}}},
  }}
  return create_UIBox_generic_options({contents = {
    seed,
    mute_button('music', 'scene_toggle_music'),
    mute_button('sfx', 'scene_toggle_sfx'),
  }})
end

-- ===== Game over / You win: no Main Menu (no menu stage here: it left an empty table with no way to a new run) =====
-- New Run stays: the game's own New Run screen (deck, stake, challenges).
local function buttons_in(n, out)
  if n.config and n.config.button then out[n.config.button] = true end
  for _, c in pairs(n.nodes or {}) do buttons_in(c, out) end
  return out
end
local function drop_main_menu(n)
  for k, c in pairs(n.nodes or {}) do  -- pairs: these node lists have holes (`cond and node or nil`)
    local b = buttons_in(c, {})
    if b.go_to_menu then
      b.go_to_menu = nil
      if next(b) == nil then n.nodes[k] = nil else drop_main_menu(c) end
    end
  end
  return n
end
for _, name in ipairs{'create_UIBox_game_over', 'create_UIBox_win'} do
  local orig = _G[name]
  _G[name] = function(...) return drop_main_menu(orig(...)) end
end

-- ===== tall layout =====
-- A tall/narrow pane moves the HUD sidebar into a horizontal strip BELOW the play field and shifts the play field left
-- (set_screen_positions derives everything from G.TILE_W), so the table is ~square and the cards render bigger.
-- Python: SC.layout_size(tall) -> w, h of the room box to show; SC.set_layout(tall) -> applied? (false while busy).
SC.tall = SC.tall or false
SC.W0 = SC.W0 or G.TILE_W             -- the wide TILE_W (20)
local TALL_SHIFT = 4.61               -- wide jokers.x (4.76) - 0.15: the play field then starts 0.15 from the left edge
local TALL_GAP = 0.4                  -- room bottom -> strip top: the deck's n/52 label hangs to 11.78, plus ~0.1
-- the dark band runs from the strip top (11.65) to the view bottom (ROOM_ORIG.y 0.7 below layout h): the 3.64 high content
-- sits in it with the same margin (0.39) above (0.11 of frame padding + TOP_PAD) and below
local TOP_PAD = 0.25
local STRIP_H = 3.72
local BOTTOM_EXT = 3                  -- the strip's dark background continues below the view like the sidebar does above/below

function SC.layout_size(tall)
  if tall then return SC.W0 - TALL_SHIFT, G.TILE_H + TALL_GAP + STRIP_H end
  return SC.W0, G.TILE_H
end

-- create_UIBox_HUD: the game's own body (UI_definitions.lua), only the returned root differs in tall mode
function create_UIBox_HUD()
    local scale = 0.4
    local stake_sprite = get_stake_sprite(G.GAME.stake or 1, 0.5)

    local contents = {}

    local spacing = 0.13
    local temp_col = G.C.DYN_UI.BOSS_MAIN
    local temp_col2 = G.C.DYN_UI.BOSS_DARK
            contents.round = {
              {n=G.UIT.R, config={align = "cm"}, nodes={
                {n=G.UIT.C, config={id = 'hud_hands',align = "cm", padding = 0.05, minw = 1.45, colour = temp_col, emboss = 0.05, r = 0.1}, nodes={
                  {n=G.UIT.R, config={align = "cm", minh = 0.33, maxw = 1.35}, nodes={
                    {n=G.UIT.T, config={text = localize('k_hud_hands'), scale = 0.85*scale, colour = G.C.UI.TEXT_LIGHT, shadow = true}},
                  }},
                  {n=G.UIT.R, config={align = "cm", r = 0.1, minw = 1.2, colour = temp_col2}, nodes={
                    {n=G.UIT.O, config={object = DynaText({string = {{ref_table = G.GAME.current_round, ref_value = 'hands_left'}}, font = G.LANGUAGES['en-us'].font, colours = {G.C.BLUE},shadow = true, rotate = true, scale = 2*scale}),id = 'hand_UI_count'}},
                  }}
                }},
                {n=G.UIT.C, config={minw = spacing},nodes={}},
                {n=G.UIT.C, config={align = "cm", padding = 0.05, minw = 1.45, colour = temp_col, emboss = 0.05, r = 0.1}, nodes={
                  {n=G.UIT.R, config={align = "cm", minh = 0.33, maxw = 1.35}, nodes={
                    {n=G.UIT.T, config={text = localize('k_hud_discards'), scale = 0.85*scale, colour = G.C.UI.TEXT_LIGHT, shadow = true}},
                  }},
                  {n=G.UIT.R, config={align = "cm"}, nodes={
                    {n=G.UIT.R, config={align = "cm", r = 0.1, minw = 1.2, colour = temp_col2}, nodes={
                      {n=G.UIT.O, config={object = DynaText({string = {{ref_table = G.GAME.current_round, ref_value = 'discards_left'}}, font = G.LANGUAGES['en-us'].font, colours = {G.C.RED},shadow = true, rotate = true, scale = 2*scale}),id = 'discard_UI_count'}},
                    }}
                  }},
                }},
              }},
              {n=G.UIT.R, config={minh = spacing},nodes={}},
              {n=G.UIT.R, config={align = "cm"}, nodes={
                {n=G.UIT.C, config={align = "cm", padding = 0.05, minw = 1.45*2 + spacing, minh = 1.15, colour = temp_col, emboss = 0.05, r = 0.1}, nodes={
                  {n=G.UIT.R, config={align = "cm"}, nodes={
                    {n=G.UIT.C, config={align = "cm", r = 0.1, minw = 1.28*2+spacing, minh = 1, colour = temp_col2}, nodes={
                      {n=G.UIT.O, config={object = DynaText({string = {{ref_table = G.GAME, ref_value = 'dollars', prefix = localize('$')}}, maxw = 1.35, colours = {G.C.MONEY}, font = G.LANGUAGES['en-us'].font, shadow = true,spacing = 2, bump = true, scale = 2.2*scale}), id = 'dollar_text_UI'}}
                  }},
                  }},
                }},
            }},
            {n=G.UIT.R, config={minh = spacing},nodes={}},
            {n=G.UIT.R, config={align = "cm"}, nodes={
              {n=G.UIT.C, config={id = 'hud_ante',align = "cm", padding = 0.05, minw = 1.45, minh = 1, colour = temp_col, emboss = 0.05, r = 0.1}, nodes={
                {n=G.UIT.R, config={align = "cm", minh = 0.33, maxw = 1.35}, nodes={
                  {n=G.UIT.T, config={text = localize('k_ante'), scale = 0.85*scale, colour = G.C.UI.TEXT_LIGHT, shadow = true}},
                }},
                {n=G.UIT.R, config={align = "cm", r = 0.1, minw = 1.2, colour = temp_col2}, nodes={
                  {n=G.UIT.O, config={object = DynaText({string = {{ref_table = G.GAME.round_resets, ref_value = 'ante'}}, colours = {G.C.IMPORTANT},shadow = true, font = G.LANGUAGES['en-us'].font, scale = 2*scale}),id = 'ante_UI_count'}},
                  {n=G.UIT.T, config={text = " ", scale = 0.3*scale}},
                  {n=G.UIT.T, config={text = "/ ", scale = 0.7*scale, colour = G.C.WHITE, shadow = true}},
                  {n=G.UIT.T, config={ref_table = G.GAME, ref_value='win_ante', scale = scale, colour = G.C.WHITE, shadow = true}}
                }},
              }},
              {n=G.UIT.C, config={minw = spacing},nodes={}},
              {n=G.UIT.C, config={align = "cm", padding = 0.05, minw = 1.45, minh = 1, colour = temp_col, emboss = 0.05, r = 0.1}, nodes={
                {n=G.UIT.R, config={align = "cm", maxw = 1.35}, nodes={
                  {n=G.UIT.T, config={text = localize('k_round'), minh = 0.33, scale = 0.85*scale, colour = G.C.UI.TEXT_LIGHT, shadow = true}},
                }},
                {n=G.UIT.R, config={align = "cm", r = 0.1, minw = 1.2, colour = temp_col2, id = 'row_round_text'}, nodes={
                  {n=G.UIT.O, config={object = DynaText({string = {{ref_table = G.GAME, ref_value = 'round'}}, colours = {G.C.IMPORTANT},shadow = true, scale = 2*scale}),id = 'round_UI_count'}},
                }},
              }},
            }},
    }

    contents.hand =
        {n=G.UIT.R, config={align = "cm", id = 'hand_text_area', colour = darken(G.C.BLACK, 0.1), r = 0.1, emboss = 0.05, padding = 0.03}, nodes={
            {n=G.UIT.C, config={align = "cm"}, nodes={
              {n=G.UIT.R, config={align = "cm", minh = 1.1}, nodes={
                {n=G.UIT.O, config={id = 'hand_name', func = 'hand_text_UI_set',object = DynaText({string = {{ref_table = G.GAME.current_round.current_hand, ref_value = "handname_text"}}, colours = {G.C.UI.TEXT_LIGHT}, shadow = true, float = true, scale = scale*1.4})}},
                {n=G.UIT.O, config={id = 'hand_chip_total', func = 'hand_chip_total_UI_set',object = DynaText({string = {{ref_table = G.GAME.current_round.current_hand, ref_value = "chip_total_text"}}, colours = {G.C.UI.TEXT_LIGHT}, shadow = true, float = true, scale = scale*1.4})}},
                {n=G.UIT.T, config={ref_table = G.GAME.current_round.current_hand, ref_value='hand_level', scale = scale, colour = G.C.UI.TEXT_LIGHT, id = 'hand_level', shadow = true}}
              }},
              {n=G.UIT.R, config={align = "cm", minh = 1, padding = 0.1}, nodes={
                {n=G.UIT.C, config={align = "cr", minw = 2, minh =1, r = 0.1,colour = G.C.UI_CHIPS, id = 'hand_chip_area', emboss = 0.05}, nodes={
                    {n=G.UIT.O, config={func = 'flame_handler',no_role = true, id = 'flame_chips', object = Moveable(0,0,0,0), w = 0, h = 0}},
                    {n=G.UIT.O, config={id = 'hand_chips', func = 'hand_chip_UI_set',object = DynaText({string = {{ref_table = G.GAME.current_round.current_hand, ref_value = "chip_text"}}, colours = {G.C.UI.TEXT_LIGHT}, font = G.LANGUAGES['en-us'].font, shadow = true, float = true, scale = scale*2.3})}},
                    {n=G.UIT.B, config={w=0.1,h=0.1}},
                }},
                {n=G.UIT.C, config={align = "cm"}, nodes={
                  {n=G.UIT.T, config={text = "X", lang = G.LANGUAGES['en-us'], scale = scale*2, colour = G.C.UI_MULT, shadow = true}},
                }},
                {n=G.UIT.C, config={align = "cl", minw = 2, minh=1, r = 0.1,colour = G.C.UI_MULT, id = 'hand_mult_area', emboss = 0.05}, nodes={
                  {n=G.UIT.O, config={func = 'flame_handler',no_role = true, id = 'flame_mult', object = Moveable(0,0,0,0), w = 0, h = 0}},
                  {n=G.UIT.B, config={w=0.1,h=0.1}},
                  {n=G.UIT.O, config={id = 'hand_mult', func = 'hand_mult_UI_set',object = DynaText({string = {{ref_table = G.GAME.current_round.current_hand, ref_value = "mult_text"}}, colours = {G.C.UI.TEXT_LIGHT}, font = G.LANGUAGES['en-us'].font, shadow = true, float = true, scale = scale*2.3})}},
                }}
              }}
            }}
          }}
    contents.dollars_chips = {n=G.UIT.R, config={align = "cm",r=0.1, padding = 0,colour = G.C.DYN_UI.BOSS_MAIN, emboss = 0.05, id = 'row_dollars_chips'}, nodes={
      {n=G.UIT.C, config={align = "cm", padding = 0.1}, nodes={
        {n=G.UIT.C, config={align = "cm", minw = 1.3}, nodes={
          {n=G.UIT.R, config={align = "cm", padding = 0, maxw = 1.3}, nodes={
            {n=G.UIT.T, config={text = localize('k_round'), scale = 0.42, colour = G.C.UI.TEXT_LIGHT, shadow = true}}
          }},
          {n=G.UIT.R, config={align = "cm", padding = 0, maxw = 1.3}, nodes={
            {n=G.UIT.T, config={text =localize('k_lower_score'), scale = 0.42, colour = G.C.UI.TEXT_LIGHT, shadow = true}}
          }}
        }},
        {n=G.UIT.C, config={align = "cm", minw = 3.3, minh = 0.7, r = 0.1, colour = G.C.DYN_UI.BOSS_DARK}, nodes={
          {n=G.UIT.O, config={w=0.5,h=0.5 , object = stake_sprite, hover = true, can_collide = false}},
          {n=G.UIT.B, config={w=0.1,h=0.1}},
          {n=G.UIT.T, config={ref_table = G.GAME, ref_value = 'chips_text', lang = G.LANGUAGES['en-us'], scale = 0.85, colour = G.C.WHITE, id = 'chip_UI_count', func = 'chip_UI_set', shadow = true}}
        }}
      }}
    }}

    contents.buttons = {
      {n=G.UIT.C, config={align = "cm", r=0.1, colour = G.C.CLEAR, shadow = true, id = 'button_area', padding = 0.2}, nodes={
          {n=G.UIT.R, config={id = 'run_info_button', align = "cm", minh = 1.75, minw = 1.5,padding = 0.05, r = 0.1, hover = true, colour = G.C.RED, button = "run_info", shadow = true}, nodes={
            {n=G.UIT.R, config={align = "cm", padding = 0, maxw = 1.4}, nodes={
              {n=G.UIT.T, config={text = localize('b_run_info_1'), scale = 1.2*scale, colour = G.C.UI.TEXT_LIGHT, shadow = true}}
            }},
            {n=G.UIT.R, config={align = "cm", padding = 0, maxw = 1.4}, nodes={
              {n=G.UIT.T, config={text = localize('b_run_info_2'), scale = 1*scale, colour = G.C.UI.TEXT_LIGHT, shadow = true, focus_args = {button = G.F_GUIDE and 'guide' or 'back', orientation = 'bm'}, func = 'set_button_pip'}}
            }}
          }},
          {n=G.UIT.R, config={align = "cm", minh = 1.75, minw = 1.5,padding = 0.05, r = 0.1, hover = true, colour = G.C.ORANGE, button = "options", shadow = true}, nodes={
            {n=G.UIT.C, config={align = "cm", maxw = 1.4, focus_args = {button = 'start', orientation = 'bm'}, func = 'set_button_pip'}, nodes={
              {n=G.UIT.T, config={text = localize('b_options'), scale = scale, colour = G.C.UI.TEXT_LIGHT, shadow = true}}
            }},
          }}
        }}
    }

    if SC.tall then
      -- squish (tall only): everything must fit the HUD_blind box (3.64 high), the tallest column
      local btn = contents.buttons[1]
      btn.config.padding = 0.1
      btn.nodes[1].config.minh, btn.nodes[2].config.minh = 1.3, 1.3
      contents.round[2].config.minh, contents.round[4].config.minh = 0.06, 0.06
      local dollars = contents.round[3].nodes[1]
      dollars.config.minh = 0.9
      dollars.nodes[1].nodes[1].config.minh = 0.75
      -- [row_blind] [dollars_chips over hand] [row_round]; the dark rows are far wider than the view and the bottom row
      -- far taller, so no edge shows left, right or below (like the sidebar's minh = 30)
      return {n=G.UIT.ROOT, config = {align = "cm", padding = 0.03, colour = G.C.UI.TRANSPARENT_DARK}, nodes={
        {n=G.UIT.R, config = {align = "cm", padding= 0.05, colour = G.C.DYN_UI.MAIN, r=0.1, minw = 60}, nodes={
          {n=G.UIT.R, config={align = "cm", colour = G.C.DYN_UI.BOSS_DARK, r=0.1, minw = 60, padding = 0.03}, nodes={
            {n=G.UIT.R, config={minh = TOP_PAD}, nodes={}},
            {n=G.UIT.R, config={align = "cm"}, nodes={
              {n=G.UIT.C, config={align = "cm"}, nodes={
                {n=G.UIT.R, config={align = "cm", id = 'row_blind', minw = 4.93, minh = 3.64}, nodes={}},
              }},
              {n=G.UIT.C, config={align = "cm"}, nodes={contents.dollars_chips, contents.hand}},
              {n=G.UIT.C, config={align = "cm", id = 'row_round'}, nodes={
                {n=G.UIT.R, config={align = "cm"}, nodes={
                  {n=G.UIT.C, config={align = "cm"}, nodes=contents.buttons},
                  {n=G.UIT.C, config={align = "cm"}, nodes=contents.round}
                }}
              }},
            }},
            {n=G.UIT.R, config={minh = BOTTOM_EXT}, nodes={}},
          }}
        }}
      }}
    end

    return {n=G.UIT.ROOT, config = {align = "cm", padding = 0.03, colour = G.C.UI.TRANSPARENT_DARK}, nodes={
      {n=G.UIT.R, config = {align = "cm", padding= 0.05, colour = G.C.DYN_UI.MAIN, r=0.1}, nodes={
        {n=G.UIT.R, config={align = "cm", colour = G.C.DYN_UI.BOSS_DARK, r=0.1, minh = 30, padding = 0.08}, nodes={
          {n=G.UIT.R, config={align = "cm", minh = 0.3}, nodes={}},
          {n=G.UIT.R, config={align = "cm", id = 'row_blind', minw = 1, minh = 3.75}, nodes={}},
          contents.dollars_chips,
          contents.hand,
          {n=G.UIT.R, config={align = "cm", id = 'row_round'}, nodes={
            {n=G.UIT.C, config={align = "cm"}, nodes=contents.buttons},
            {n=G.UIT.C, config={align = "cm"}, nodes=contents.round}
          }},
        }}
      }}
    }}
end

-- Game:start_run's HUD (and the refs it keeps into it) for the current mode
local function hud_refs()
  G.hand_text_area = {
    chips = G.HUD:get_UIE_by_ID('hand_chips'),
    mult = G.HUD:get_UIE_by_ID('hand_mult'),
    ante = G.HUD:get_UIE_by_ID('ante_UI_count'),
    round = G.HUD:get_UIE_by_ID('round_UI_count'),
    chip_total = G.HUD:get_UIE_by_ID('hand_chip_total'),
    handname = G.HUD:get_UIE_by_ID('hand_name'),
    hand_level = G.HUD:get_UIE_by_ID('hand_level'),
    game_chips = G.HUD:get_UIE_by_ID('chip_UI_count'),
    blind_chips = G.HUD_blind:get_UIE_by_ID('HUD_blind_count'),
    blind_spacer = G.HUD:get_UIE_by_ID('blind_spacer')
  }
end

-- The game hides the boxes hung on row_blind (HUD_blind, the blind prompt, the shop sign) with offset.y -10 / -15: above
-- the sidebar, off-screen.  With the strip at the bottom that is inside the play field, so in tall mode a hide offset
-- (y <= -5) reads as +y: below the strip.  The game only writes these offsets, never reads them back.
-- `box.alignment.offset.y` as the engine reads it: read(stored y); the game only writes these offsets
local function offset_as(box, read)
  local y = box.alignment.offset.y
  box.alignment.offset = setmetatable({x = box.alignment.offset.x}, {
    __index = function(_, k) if k == 'y' then return read(y) end end,
    __newindex = function(t, k, v) if k == 'y' then y = v else rawset(t, k, v) end end})
end

local function hide_below(box)
  if not box then return end
  offset_as(box, function(y) return SC.tall and y <= -5 and -y or y end)
  if SC.tall then box:align_to_major(); box:hard_set_VT(); box:recalculate() end  -- recalculate: the elements follow the box
end

-- Full-screen overlays (G.FUNCS.overlay_menu) centre on ROOM_ATTACH, the 11.5 high room: tall, the view is taller
local overlay_menu = SC.overlay_menu or G.FUNCS.overlay_menu
SC.overlay_menu = overlay_menu
function G.FUNCS.overlay_menu(args)
  overlay_menu(args)
  if G.OVERLAY_MENU then offset_as(G.OVERLAY_MENU, function(y) return SC.tall and y + (select(2, SC.layout_size(true)) - G.TILE_H) / 2 or y end) end
end

local function rebuild_hud()
  local old, hung = G.HUD, {}
  for _, b in ipairs(G.I.UIBOX) do
    local m = b ~= old and b.role and b.role.major
    if m and m.UIBox == old then hung[#hung + 1] = {b, m.config.id, b.role.xy_bond} end
  end
  old:remove()
  G.HUD = UIBox{
    definition = create_UIBox_HUD(),
    config = SC.tall and {align = 'bm', offset = {x = 0, y = TALL_GAP}, major = G.ROOM_ATTACH}
      or {align = 'cli', offset = {x = -0.7, y = 0}, major = G.ROOM_ATTACH}}
  for _, h in ipairs(hung) do
    local b, new = h[1], G.HUD:get_UIE_by_ID(h[2])
    if new then
      b:set_alignment{major = new, bond = h[3]}
      b.alignment.prev_offset = {}
      b:align_to_major()
      b:hard_set_VT()
      b:recalculate()
    end
  end
  hud_refs()
end

-- TILE_W, the room's size and the padding of the current mode.  Game:prep_stage (start_run) runs love.resize, which centres
-- the room for G.TILE_W in the window: tall must put its own padding back
local function apply_room()
  SC.wide = SC.wide or {x = G.ROOM_ORIG.x, y = G.ROOM_ORIG.y, r = G.ROOM_ORIG.r}  -- the game's own wide padding
  G.TILE_W = SC.tall and SC.W0 - TALL_SHIFT or SC.W0
  G.ROOM.T.w, G.ROOM_ATTACH.T.w = G.TILE_W, G.TILE_W
  -- tall: the game's own ROOM_PADDING (1 x 0.7); the skip tags hang 0.7 past the room's right edge
  local p = SC.tall and {x = G.ROOM_PADDING_W, y = G.ROOM_PADDING_H, r = SC.wide.r} or SC.wide
  G.ROOM.T.x, G.ROOM.T.y = p.x, p.y
  G.ROOM_ORIG = {x = p.x, y = p.y, r = p.r}
  G.ROOM_ATTACH:hard_set_VT()
  -- Controller:get_cursor_collision ignores a cursor more than DRAW_HASH_BUFF (2) below the room: the strip is 4.0 below
  G.DRAW_HASH_BUFF = SC.tall and 4.5 or 2
end

local start_run = SC.start_run or Game.start_run
SC.start_run = start_run
function Game:start_run(args)
  start_run(self, args)
  hide_below(G.HUD_blind)
  if SC.tall then apply_room(); rebuild_hud() end
end

for _, w in ipairs{{_G, 'create_UIBox_blind_select', 'blind_prompt_box'}, {G.UIDEF, 'shop', 'SHOP_SIGN'}} do
  local t, name, box = w[1], w[2], w[3]
  local orig = SC['orig_' .. name] or t[name]
  SC['orig_' .. name] = orig
  t[name] = function(...)
    local r = orig(...)
    hide_below(G[box])
    return r
  end
end

function SC.set_layout(tall)
  tall = tall and true or false
  if tall == SC.tall then return true end
  if BT.busy() then return false end
  SC.wide = SC.wide or {x = G.ROOM_ORIG.x, y = G.ROOM_ORIG.y, r = G.ROOM_ORIG.r}
  SC.tall = tall
  apply_room()
  if G.HUD then
    rebuild_hud()
    set_screen_positions()
    -- the areas' backing boxes (children.area_uibox) keep the offset they were aligned with: CardArea:draw rebuilds them in place
    for _, a in ipairs(G.I.CARDAREA) do
      local b = a.children.area_uibox
      if b then b:remove(); a.children.area_uibox = nil end
    end
  end
  return true
end

-- Deck view (Full Deck / Remaining tabs): wide it is one row [info | rank column | 4 suit areas] ~20 wide and 7.3 high.
-- Tall: the suit areas on top; under them the deck panel, the tallies (4 suits in one row) and the rank column (split
-- in two) side by side.  Rearranges the game's own nodes; only paddings shrink, the cards keep their scale.
SC.view_deck = SC.view_deck or G.UIDEF.view_deck
function G.UIDEF.view_deck(...)
  local t = SC.view_deck(...)
  if not SC.tall then return t end
  local row = t.nodes[2]                 -- {info + ranks panel, spacer, suit areas}
  local left, areas = row.nodes[1], row.nodes[3]
  local deck, tallies = left.nodes[1].nodes[1], left.nodes[1].nodes[2]
  local suits = tallies.nodes[3].nodes   -- {Spades, Hearts}; Clubs, Diamonds are the next row
  suits[3], suits[4] = tallies.nodes[4].nodes[1], tallies.nodes[4].nodes[2]
  tallies.nodes[4] = nil
  local ranks = left.nodes[2].nodes      -- 13 rows, A .. 2
  left.nodes = {
    {n = G.UIT.C, config = {align = "cm", padding = 0.1}, nodes = {deck}},
    {n = G.UIT.C, config = {align = "cm", padding = 0.1}, nodes = {tallies}},
    {n = G.UIT.C, config = {align = "cm"}, nodes = {unpack(ranks, 1, 7)}},
    {n = G.UIT.C, config = {align = "cm"}, nodes = {unpack(ranks, 8, 13)}},
    {n = G.UIT.B, config = {w = 0.1, h = 0.1}}}
  t.nodes[3].config.minh = 0.3           -- the notes row
  t.nodes = {t.nodes[1], {n = G.UIT.R, config = {align = "cm"}, nodes = {areas}},
    {n = G.UIT.R, config = {align = "cm", minh = 0.1}, nodes = {}}, {n = G.UIT.R, config = {align = "cm"}, nodes = {left}}, t.nodes[3]}
  return t
end
