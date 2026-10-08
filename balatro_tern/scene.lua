-- scene.lua: walks every live UIBox (HUD, blind select, shop, cash out, overlays, tooltips ...) and packs what the
-- game's own UI engine drew into two parallel strings; scene.py turns them into TSP nodes + a per-box stylesheet.
--   SC.frame() -> S, F      S = structure (changes => rebuild nodes), F = per-frame geometry/colours, line i of F is line i of S
--   S lines:  B \t boxid \t layer                        a UIBox (layer u|a|c|o|p, bottom -> top draw order)
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
    -- Only while nothing else (quiver, pulse, a letter's scale) feeds the offset.
    local mode, rate, amp, wob = '', 0, 0, ''
    if not G.SETTINGS.reduced_motion and not obj.config.quiver and not obj.config.pulse then
      if obj.config.bump then
        mode, rate, amp = 'b', obj.bump_rate, obj.bump_amount * math.sqrt(sc) * 7
      elseif obj.config.float then
        mode, rate, amp = 'f', 2.666, math.sqrt(sc) * k * 2000
        for _, l in ipairs(letters) do if math.abs((l.scale or 1) - 1) > 1e-3 then mode = '' end end
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
    -- every string is in the structure (text + colours per string: cycling strings only change `focused`, the node tree
    -- stays), the per-letter frame data is the focused string's
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
    local desc = table.concat({'D', table.concat(texts, '\5'), table.concat(colss, '\5'), sh, f3(sc), '0'}, '\3')
    return desc, table.concat({f3(vt.x), f3(vt.y), f3(vt.w), f3(vt.h), f3(vt.scale), f3(vt.r), f3(ox), f3(oy), table.concat(per, ';'),
      f3(-px * sc / G.TILESIZE), f3(-py * sc / G.TILESIZE), fmt('%.4f', nx), fmt('%.4f', ny), mode, fmt('%.4f', rate), fmt('%.4f', -0.5 * amp * k / sc), wob,
      obj.focused_string - 1}, '\3')
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
-- < attention_text boxes < overlay menu < popups
local function layer_of(b)
  if b == G.OVERLAY_MENU then return 'o' end
  if b.attention_text then return 't' end
  local p = b.config and b.config.parent
  -- Card:draw draws its focus frame (card_focus_ui) before the card, except in the hand, and its price tag and
  -- buy / use buttons before its sprite too (they peek out from behind the card)
  if p and p.children and p.children.focused_ui == b and p.area ~= G.hand then return 'f' end
  local ch = p and p.children
  if ch and (ch.price == b or ch.buy_button == b or ch.buy_and_use_button == b or ch.use_button == b) then return 'f' end
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

local function dump_box(b, layer, S, Fo)
  S[#S + 1] = table.concat({'B', b.ID, layer}, '\t')
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
    list[#list + 1] = {b, layer or layer_of(b)}
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
  -- stable sort by layer (table.sort is not stable: tag with the index)
  for i, v in ipairs(list) do v[4] = i end
  table.sort(list, function(a, b) if a[2] ~= b[2] then return ORDER[a[2]] < ORDER[b[2]] end return a[4] < b[4] end)
  local S, Fo = {}, {}
  local i = 1
  while i <= #list do
    local b, layer = list[i][1], list[i][2]
    if list[i][3] == 'P' then
      dump_emitter(b, layer, S, Fo)
    else
      local nested = dump_box(b, layer, S, Fo)
      for _, nb in ipairs(nested) do if not seen[nb] then seen[nb] = true; list[#list + 1] = {nb, layer, nil, #list + 1} end end
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
