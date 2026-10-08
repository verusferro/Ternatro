-- love.* stubs + Balatro bridge (module `BT`). Loaded by game.py after it sets the Lua globals
-- ROOT (game dir, trailing /), DATA (save dir, trailing /), PYLS(path), PYSTAT(path), PYMKDIR(path).
package.path = ROOT..'?.lua;'..package.path
local stub
stub = setmetatable({}, {__index=function() return stub end, __call=function() return stub end})
local function fread(base, p) local f = io.open(base..p, 'rb'); if not f then return nil end local s = f:read('*a'); f:close(); return s end
local chan = setmetatable({}, {__index=function() return function() end end})
local function wr(p, s, mode)
  PYMKDIR(DATA..(p:match('^(.*)/[^/]*$') or ''))
  local f = assert(io.open(DATA..p, mode)); f:write(s); f:close(); return true
end
love = {
  system = {getOS=function() return 'Linux' end, getProcessorCount=function() return 1 end, openURL=function() end},
  filesystem = {  -- reads: DATA first, then game dir; writes: DATA only (game's real save files live there)
    getInfo = function(p) local t = PYSTAT(DATA..p) or PYSTAT(ROOT..p); if t then return {type=t} end end,
    read = function(p) return fread(DATA, p) or fread(ROOT, p) end,
    write = function(p, s) return wr(p, s, 'wb') end,
    append = function(p, s) return wr(p, s, 'ab') end,
    getDirectoryItems = function(p) return PYLS(ROOT..p) end,
    createDirectory = function(p) PYMKDIR(DATA..p) end,
    remove = function(p) return os.remove(DATA..p) end,
    getSourceBaseDirectory = function() return ROOT end,
  },
  data = {compress=function(_, _, s) return s end, decompress=function(_, _, s) return s end},  -- saves are plain `return {...}` (get_compressed accepts it)
  timer = {getTime=os.clock, step=function() return 0 end, sleep=function() end, getFPS=function() return 60 end},
  thread = {newThread=function() return {start=function() end} end, getChannel=function() return chan end},
  graphics=stub, window=stub, audio=stub, mouse=stub, joystick=stub, event=stub, touch=stub, keyboard=stub, sound=stub,
}
love.graphics = setmetatable({getWidth=function() return 1920 end, getHeight=function() return 1080 end,
  getDimensions=function() return 1920, 1080 end, isActive=function() return false end},
  {__index=function() return function() return stub end end})
love.window = setmetatable({getMode=function() return 1920, 1080, {} end, getDesktopDimensions=function() return 1920, 1080 end,
  toPixels=function(x) return x end, getFullscreenModes=function() return {} end, getTitle=function() return 'x' end},
  {__index=function() return function() return stub end end})
-- real glyph metrics (PIL) so UIBox layout (T.w/T.h from Font:getWidth/getHeight) matches the game
local font = {getWidth=function(self, c) return PYFONTW(self.file, self.size, tostring(c)) end, getHeight=function(self) return PYFONTH(self.file, self.size) end,
  getBaseline=function() return 12 end, getAscent=function() return 12 end, getDescent=function() return 4 end,
  setFilter=function() end, getLineHeight=function() return 1 end}
font.__index = font
love.graphics.newFont = function(file, size) return setmetatable({file = ROOT..tostring(file), size = size or 12}, font) end
love.graphics.newText = function() return setmetatable({getDimensions=function() return 8, 16 end, getWidth=function() return 8 end, getHeight=function() return 16 end, set=function() end}, {__index=function() return function() end end}) end
love.joystick.loadGamepadMappings = function() end
love.joystick.getJoysticks = function() return {} end
love.mouse.getPosition = function() return 0, 0 end
love.mouse.isVisible = function() return false end
love.touch.getTouches = function() return {} end
package.preload['https'] = function() return stub end
package.preload['luasteam'] = function() return stub end

dofile(ROOT..'main.lua')  -- defines G (love.run is never invoked)

BT = {}
local FEED = {}
local NULL = setmetatable({}, {__tostring=function() return 'null' end})
local function N(v) if v == nil then return NULL end return v end
local function obj(t) return setmetatable(t, {__obj=true}) end

-- ===== JSON (no nils: use N()); {} -> [] unless obj() =====
local function jstr(s) return '"'..(s:gsub('[%c"\\]', function(c) return string.format('\\u%04x', c:byte()) end))..'"' end
local function enc(v, o)
  local t = type(v)
  if v == NULL or v == nil then o[#o+1] = 'null'
  elseif t == 'string' then o[#o+1] = jstr(v)
  elseif t == 'number' then
    if v ~= v then o[#o+1] = 'null' elseif v > 1e308 then o[#o+1] = '1e308' elseif v < -1e308 then o[#o+1] = '-1e308' else o[#o+1] = string.format('%.14g', v) end
  elseif t == 'boolean' then o[#o+1] = tostring(v)
  elseif t == 'table' then
    local mt = getmetatable(v)
    if (mt and mt.__obj) or (v[1] == nil and next(v) ~= nil) then
      o[#o+1] = '{'; local first = true
      for k, x in pairs(v) do
        if not first then o[#o+1] = ',' end; first = false
        o[#o+1] = jstr(tostring(k)); o[#o+1] = ':'; enc(x, o)
      end
      o[#o+1] = '}'
    else
      o[#o+1] = '['
      for i = 1, #v do if i > 1 then o[#o+1] = ',' end; enc(v[i], o) end
      o[#o+1] = ']'
    end
  else o[#o+1] = 'null' end
end
local function json(v) local o = {}; enc(v, o); return table.concat(o) end

-- ===== boot =====
local function unlock_all()
  local P = G.PROFILES[G.SETTINGS.profile]
  P.all_unlocked = true
  for _, tbl in ipairs{G.P_CENTERS, G.P_BLINDS, G.P_TAGS, G.P_SEALS} do
    for _, v in pairs(tbl) do
      if not v.demo and not v.wip then v.alerted = true; v.discovered = true; v.unlocked = true end
    end
  end
  G.P_LOCKED = {}
  table.sort(G.P_CENTER_POOLS['Back'], function(a, b) return (a.order - (a.unlocked and 100 or 0)) < (b.order - (b.unlocked and 100 or 0)) end)
  set_profile_progress(); set_discover_tallies()
end

local function wrap_feed()
  local o_att, o_hand, o_juice = attention_text, update_hand_text, juice_card
  local function push(e) if #FEED < 3000 then FEED[#FEED+1] = e end end
  attention_text = function(args)
    local m = args and args.major
    local text = args and args.text
    if type(text) == 'table' then text = nil end
    if m and type(m) == 'table' and m.sort_id and m.ability and args.backdrop_colour and text then
      local c, bc = 'other', args.backdrop_colour
      if bc == G.C.CHIPS then c = 'chips' elseif bc == G.C.MULT then c = 'mult' elseif bc == G.C.XMULT then c = 'xmult'
      elseif bc == G.C.MONEY or tostring(text):find('^%-?%$') then c = 'money' end
      push{kind='eval', card_id=m.sort_id, text=tostring(text), colour=c}
    elseif text then push{kind='attention', text=tostring(text)} end
    return o_att(args)
  end
  update_hand_text = function(config, vals)
    if vals then push{kind='hand', name=vals.handname, chips=vals.chips, mult=vals.mult, level=vals.level} end
    return o_hand(config, vals)
  end
  juice_card = function(card) if card and card.sort_id then push{kind='juice', card_id=card.sort_id} end return o_juice(card) end
  chan.push = function(_, t)  -- everything the game sends to its sound thread
    if type(t) ~= 'table' then return end
    local s = t.sound_settings
    local vols = s and {s.volume, s.music_volume, s.game_sounds_volume}
    if t.type == 'sound' then push{kind='sound', name=t.sound_code, pitch=t.per, volume=t.vol, vols=vols}
    elseif t.type == 'modulate' then
      local amb = {}
      for k, v in pairs(t.ambient_control or {}) do amb[k] = obj{vol=v.vol, per=v.per} end
      local e = {kind='modulate', track=t.desired_track, pitch_mod=t.pitch_mod, dt=t.dt, ambient=obj(amb), vols=vols}
      local p = FEED[#FEED]; if p and p.kind == 'modulate' then e.dt = (e.dt or 0) + (p.dt or 0); FEED[#FEED] = e else push(e) end  -- fires every update: coalesce consecutive ones
    end
  end
end

function BT.boot(speed)
  G:start_up()
  -- save thread inline: only the run save matters (progress/settings are never persisted; profile is "everything unlocked")
  G.SAVE_MANAGER = {channel = {push = function(_, req)
    if req.type == 'save_run' then compress_and_save((req.profile_num or 1)..'/save.jkr', req.save_table) end
  end}}
  G.E_MANAGER:clear_queue()  -- drop splash -> main_menu events
  G.SETTINGS.tutorial_complete = true
  G.SETTINGS.GAMESPEED = speed
  unlock_all()
  wrap_feed()
  for _ = 1, 3 do G:update(1/60) end
end

function BT.tick(dt)
  local f = G.FILE_HANDLER
  if f and f.run then f.force = true end  -- flush run saves immediately instead of every F_SAVE_TIMER seconds
  G:update(dt)
end

local STATE_NAME
local function state_name()
  if not STATE_NAME then STATE_NAME = {}; for k, v in pairs(G.STATES) do STATE_NAME[v] = k end end
  if G.STAGE ~= G.STAGES.RUN then return 'MENU' end
  return STATE_NAME[G.STATE] or tostring(G.STATE)
end

local function qlen_blocking()
  for _, k in ipairs{'base', 'unlock'} do
    for _, e in ipairs(G.E_MANAGER.queues[k]) do if e.blocking ~= false then return true end end
  end
  return false
end
function BT.busy()
  if not G.GAME or G.STAGE ~= G.STAGES.RUN then return false end
  return (G.CONTROLLER.locked and true) or (G.screenwipe and true) or ((G.GAME.STOP_USE or 0) > 0) or qlen_blocking() or false
end

-- ===== new / continue =====
function BT.new_run(seed, deck, stake)
  G.E_MANAGER:clear_queue()
  G.SETTINGS.paused = false
  if G.OVERLAY_MENU then G.OVERLAY_MENU:remove(); G.OVERLAY_MENU = nil end
  G:delete_run()
  G.GAME.viewed_back = {name = G.P_CENTERS[deck].name}
  G:start_run{stake = stake, seed = seed}
  G.SETTINGS.paused = false
end

function BT.has_save() return love.filesystem.getInfo('1/save.jkr') ~= nil end

function BT.continue_run()
  local s = get_compressed(G.SETTINGS.profile..'/save.jkr')
  if not s then return false end
  local saved = STR_UNPACK(s)
  if not saved or not saved.GAME then return false end
  G.E_MANAGER:clear_queue()
  G.SETTINGS.paused = false
  if G.OVERLAY_MENU then G.OVERLAY_MENU:remove(); G.OVERLAY_MENU = nil end
  G:delete_run()
  G:start_run{savetext = saved}
  G.SETTINGS.paused = false
  return true
end

-- ===== snapshot =====
local function areas()
  local t = {hand=G.hand, play=G.play, jokers=G.jokers, consumeables=G.consumeables, deck=G.deck, discard=G.discard}
  if G.shop then t.shop_jokers, t.shop_vouchers, t.shop_booster = G.shop_jokers, G.shop_vouchers, G.shop_booster end
  if G.booster_pack then t.pack_cards = G.pack_cards end
  local misc = {}  -- Card_Character cards (Jimbo): not in any CardArea
  local named = {}
  for _, a in pairs(t) do named[a] = true end
  for _, c in pairs(G.I.CARD) do  -- + cards living in an overlay menu's own CardArea (deck view, run info, ...)
    if not c.REMOVED and (c.jimbo or (G.OVERLAY_MENU and c.area and not named[c.area] and c.states.visible)) then misc[#misc + 1] = c end
  end
  if #misc > 0 then table.sort(misc, function(a, b) return a.sort_id < b.sort_id end); t.misc = {cards = misc, config = {}} end
  return t
end

local function xy(p) if p then return {x=p.x, y=p.y} end return NULL end

local function card_name(c)
  local ctr = c.config.center
  if c.base and c.config.card_key and ctr.key ~= 'm_stone' then return c.base.name end
  if ctr.set == 'Booster' then return localize{type='name_text', key=(ctr.key:gsub('_%d+$', '')), set='Other'} end
  return localize{type='name_text', key=ctr.key, set=ctr.set}
end

local function card_t(c)
  local ctr = c.config.center
  local st = {}
  if c.ability.eternal then st[#st+1] = 'eternal' end
  if c.ability.perishable then st[#st+1] = 'perishable' end
  if c.ability.rental then st[#st+1] = 'rental' end
  if c.pinned then st[#st+1] = 'pinned' end
  local playing = c.base and c.config.card_key and true
  local fa, fp = NULL, NULL
  if c.config.card and c.config.card.suit then
    local a, p = get_front_spriteinfo(c.config.card); fa = a and a.name or NULL; fp = xy(p)
  end
  return {
    id=c.sort_id, key=N(c.config.center_key), set=c.ability.set,
    front=N(playing and c.config.card_key or nil), rank=N(playing and c.base.value or nil), suit=N(playing and c.base.suit or nil),
    edition=N(c.edition and c.edition.type), seal=N(c.seal), stickers=st,
    highlighted=c.highlighted and true or false, debuff=c.debuff and true or false, facing=c.facing or 'front',
    cost=c.cost or 0, sell=c.sell_cost or 0, name=card_name(c),
    atlas=ctr.atlas or ((ctr.set == 'Joker' or ctr.consumeable or ctr.set == 'Voucher') and ctr.set) or 'centers', pos=xy(ctr.pos), soul_pos=xy(ctr.soul_pos), front_atlas=fa, front_pos=fp,
  }
end
function BT.card_json(c) return json(card_t(c)) end  -- scene.lua: cards drawn inside tips

local function blind_t()
  local b = G.GAME.blind
  if not b or not b.config or not b.config.blind or not b.config.blind.key or b.name == '' then return NULL end
  local bl = b.config.blind
  return {key=bl.key, name=b.loc_name or localize{type='name_text', key=bl.key, set='Blind'}, boss=b.boss and true or false,
          debuff_text=b.loc_debuff_text or '', reward=b.dollars or 0, pos=xy(bl.pos)}
end

local function choices_t()
  local r = G.GAME.round_resets
  local out = {}
  for _, ty in ipairs{'Small', 'Big', 'Boss'} do
    local st = r.blind_states and r.blind_states[ty]
    local key = r.blind_choices and r.blind_choices[ty]
    local bl = key and G.P_BLINDS[key]
    if bl and st ~= 'Hide' then
      local tag = NULL
      local tk = r.blind_tags and r.blind_tags[ty]
      if tk and G.P_TAGS[tk] then tag = {key=tk, name=localize{type='name_text', key=tk, set='Tag'}, pos=xy(G.P_TAGS[tk].pos)} end
      local reward = bl.dollars or 0
      if G.GAME.modifiers.no_blind_reward and G.GAME.modifiers.no_blind_reward[ty] then reward = 0 end
      if st == 'Current' then st = 'Select' end
      local desc = localize{type='raw_descriptions', key=key, set='Blind', vars={localize(G.GAME.current_round.most_played_poker_hand, 'poker_hands')}}
      out[#out+1] = {type=ty, key=key, name=localize{type='name_text', key=key, set='Blind'},
        chips=get_blind_amount(r.blind_ante or r.ante)*bl.mult*G.GAME.starting_params.ante_scaling,
        reward=reward, state=st, tag=tag, debuff_text=table.concat(desc, ' '), pos=xy(bl.pos)}
    end
  end
  return out
end

-- ===== action validity (uses the game's own can_* button funcs via a fake UIElement) =====
local function fe(ref, id)
  return {config={ref_table=ref, id=id}, UIBox={states={visible=true}, alignment={offset={}}}, children={}, states={visible=true}}
end
local function can(fn, ref, id) local e = fe(ref, id); G.FUNCS[fn](e); return e.config.button ~= nil, e end
local function in_state(...) for _, s in ipairs{...} do if G.STATE == G.STATES[s] then return true end end return false end
local function idle() return G.GAME and G.STAGE == G.STAGES.RUN and not G.CONTROLLER.locked and not G.screenwipe and not G.SETTINGS.paused and (G.GAME.STOP_USE or 0) <= 0 end

local function can_play() return idle() and in_state('SELECTING_HAND') and not G.play.cards[1] and (can('can_play')) end
local function can_discard() return idle() and in_state('SELECTING_HAND') and (can('can_discard')) end

local PACKS = {'TAROT_PACK', 'PLANET_PACK', 'SPECTRAL_PACK', 'STANDARD_PACK', 'BUFFOON_PACK'}
local function in_pack() return in_state(unpack(PACKS)) end

function BT.snapshot()
  if not G.GAME or G.STAGE ~= G.STAGES.RUN then
    return json{state='MENU', won=false, seed=NULL, busy=false, overlay=false, hud=NULL, areas=obj{}, blind_choices={}, shop=NULL, can={play=false, discard=false}}
  end
  local g, cr = G.GAME, G.GAME.current_round
  local ch = cr.current_hand or {}
  local hn = ch.handname
  local hand = hn and g.hands[hn]
  local ar = obj{}
  for name, a in pairs(areas()) do
    local cs = {}
    for i, c in ipairs(a.cards) do cs[i] = card_t(c) end
    ar[name] = {cards=cs, limit=a.config.card_limit or 0, highlighted_limit=a.config.highlighted_limit or 0}
  end
  local function num(x) return type(x) == 'number' and x or 0 end
  local blind = g.blind
  return json{
    state=state_name(), won=g.won and true or false, seed=N(g.pseudorandom and g.pseudorandom.seed), busy=BT.busy(), overlay=G.OVERLAY_MENU and true or false,
    hud={dollars=g.dollars, ante=g.round_resets.ante, round=g.round, hands_left=cr.hands_left, discards_left=cr.discards_left,
      chips=g.chips, target=blind and blind.chips or 0,
      hand_name=(hn and hn ~= '') and hn or NULL, hand_level=(hand and hand.level) or 0,
      hand_chips=num(ch.chips), hand_mult=num(ch.mult), blind=blind_t(),
      joker_slots=G.jokers.config.card_limit, consumable_slots=G.consumeables.config.card_limit,
      deck_count=#G.deck.cards, deck_total=#G.playing_cards},
    areas=ar, blind_choices=choices_t(),
    shop=G.shop and {reroll_cost=cr.reroll_cost, free_rerolls=cr.free_rerolls or 0} or NULL,
    can={play=can_play() and true or false, discard=can_discard() and true or false},
  }
end

-- ===== describe =====
local function txt(n)
  if type(n) ~= 'table' then return '' end
  if n.n then
    local c = n.config or {}
    if n.n == G.UIT.T then
      if c.text ~= nil then return tostring(c.text) end
      if c.ref_table and c.ref_value then return tostring(c.ref_table[c.ref_value]) end
      return ''
    elseif n.n == G.UIT.O then
      local o = c.object
      if o and o.strings and o.strings[1] then return tostring(o.strings[1].string) end
      return ''
    end
    return txt(n.nodes)
  end
  local s = {}
  for _, x in ipairs(n) do s[#s+1] = txt(x) end
  return table.concat(s)
end

local function find_card(id)
  for _, c in pairs(G.I.CARD) do if c.sort_id == id and c.ability then return c end end
end

function BT.describe(id)
  local c = find_card(id)
  if not c then return {} end
  local t = c:generate_UIBox_ability_table()
  local out = {}
  local function trim(x) return (x:gsub('^%s+', ''):gsub('%s+$', '')) end
  local nm = t.name
  if type(nm) == 'table' then nm = (nm[1] and nm[1].n) and txt(nm) or txt(nm[1]) else nm = nil end
  out[1] = trim((nm and nm ~= '') and nm or card_name(c))
  for _, line in ipairs(t.main or {}) do local s = trim(txt(line)); if s ~= '' then out[#out+1] = s end end
  for _, info in ipairs(t.info or {}) do
    local n = info.name and trim(txt(info.name)); if n and n ~= '' then out[#out+1] = '- '..n end
    for _, line in ipairs(info) do local s = trim(txt(line)); if s ~= '' then out[#out+1] = s end end
  end
  for _, b in ipairs(t.badges or {}) do
    if type(b) == 'string' then local l = G.localization.misc.labels[b]; if l then out[#out+1] = '['..l..']' end end
  end
  return out
end

function BT.drain() local j = json(FEED); FEED = {}; return j end

-- ===== actions =====
local function area_of(name) return areas()[name] end
local function card_at(area, idx) local a = area_of(area); return a and a.cards[idx + 1], a end

function BT.toggle(area, idx)
  local c, a = card_at(area, idx)
  if not c or area == 'play' or area == 'deck' or area == 'discard' then return false end
  if area == 'hand' and not in_state('SELECTING_HAND', 'TAROT_PACK', 'SPECTRAL_PACK') then return false end
  if area == 'hand' and G.CONTROLLER.locks.use then return false end
  if c.highlighted then a:remove_from_highlighted(c)
  else
    if a.config.highlighted_limit <= #a.highlighted and area == 'hand' then return false end
    a:add_to_highlighted(c)
  end
  return true
end

function BT.play() if not can_play() then return false end G.FUNCS.play_cards_from_highlighted(fe()); return true end
function BT.discard() if not can_discard() then return false end G.FUNCS.discard_cards_from_highlighted(fe()); return true end

function BT.sort_hand(by)
  if not G.hand or not G.hand.cards[1] or not idle() then return false end
  if by == 'rank' then G.FUNCS.sort_hand_value(fe()) elseif by == 'suit' then G.FUNCS.sort_hand_suit(fe()) else return false end
  return true
end

function BT.move(area, i, j)
  local a = area_of(area)
  if area ~= 'hand' and area ~= 'jokers' and area ~= 'consumeables' then return false end
  if not a or not a.cards[i+1] or not a.cards[j+1] then return false end
  table.insert(a.cards, j + 1, table.remove(a.cards, i + 1))
  a:set_ranks()
  return true
end

function BT.select_blind()
  if not (idle() and in_state('BLIND_SELECT') and G.blind_select) then return false end
  G.FUNCS.select_blind(fe(G.P_BLINDS[G.GAME.round_resets.blind_choices[G.GAME.blind_on_deck]]))
  return true
end

function BT.skip_blind()
  local ty = G.GAME and G.GAME.blind_on_deck
  if not (idle() and in_state('BLIND_SELECT') and G.blind_select and ty and ty ~= 'Boss') then return false end
  local e = fe(); e.UIBox = G.blind_select_opts[ty:lower()]
  if not e.UIBox then return false end
  G.FUNCS.skip_blind(e)
  return true
end

local function cash_out_button()
  for _, b in ipairs(G.I.UIBOX) do
    local x = b.get_UIE_by_ID and b:get_UIE_by_ID('cash_out_button'); if x then return x end
  end
end

function BT.cash_out()
  if not (idle() and in_state('ROUND_EVAL') and G.round_eval and cash_out_button()) then return false end
  G.FUNCS.cash_out(fe()); return true
end

function BT.next_round()
  if not (idle() and in_state('SHOP') and G.shop) then return false end
  G.FUNCS.toggle_shop(fe()); return true
end

function BT.reroll()
  if not (idle() and in_state('SHOP') and G.shop and (can('can_reroll'))) then return false end
  G.FUNCS.reroll_shop(fe()); return true
end

local function has_space(c)
  local neg = (c.edition and c.edition.negative) and 1 or 0
  if c.ability.set == 'Joker' then return #G.jokers.cards < G.jokers.config.card_limit + neg end
  if c.ability.consumeable then return #G.consumeables.cards < G.consumeables.config.card_limit + neg end
  return true
end

function BT.buy(area, idx, use)
  local c = card_at(area, idx)
  if not c or not (idle() and in_state('SHOP') and G.shop) then return false end
  if area == 'shop_jokers' then
    if use then
      if not c.ability.consumeable or not can('can_buy_and_use', c, 'buy_and_use') then return false end
      G.FUNCS.buy_from_shop(fe(c, 'buy_and_use'))
    else
      if not can('can_buy', c) or not has_space(c) then return false end
      G.FUNCS.buy_from_shop(fe(c))
    end
  elseif area == 'shop_vouchers' then
    if not can('can_redeem', c) then return false end
    G.FUNCS.use_card(fe(c))
  elseif area == 'shop_booster' then
    if not can('can_open', c) then return false end
    G.FUNCS.use_card(fe(c))
  else return false end
  return true
end

function BT.sell(area, idx)
  local c = card_at(area, idx)
  if not c or (area ~= 'jokers' and area ~= 'consumeables') or not idle() then return false end
  if not can('can_sell_card', c) then return false end
  G.FUNCS.sell_card(fe(c)); return true
end

function BT.use(area, idx)
  local c = card_at(area, idx)
  if not c or not idle() then return false end
  if area == 'consumeables' then
    if not can('can_use_consumeable', c) then return false end
  elseif area == 'pack_cards' and in_pack() then
    if (G.GAME.pack_choices or 0) < 1 then return false end
    if c.ability.consumeable then if not can('can_use_consumeable', c) then return false end
    elseif not can('can_select_card', c) then return false end
  else return false end
  G.FUNCS.use_card(fe(c)); return true
end

function BT.skip_pack()
  if not (idle() and in_pack() and (can('can_skip_booster'))) then return false end
  G.FUNCS.skip_booster(fe()); return true
end

function BT.button(name, ref)
  local f = G.FUNCS[name]
  if not f then return false end
  f(fe(ref)); return true
end
