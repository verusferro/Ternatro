"""Balatro's GLSL (extracted/resources/shaders/*.fs) ported to numpy and baked into looping animated WebP / PNG blobs.

    key, data, mime = bake(shader, params, size=None, frames=None, fps=None)

Deterministic and disk-cached (<data dir>/shaders, override with BALATRO_SHADER_CACHE).
`key` is a content hash (usable as a Tern blob key); `mime` is image/webp (animated) or image/png (static).

Shaders and params (all optional unless marked; colours are '#rrggbb', 'rrggbb' or (r,g,b[,a]) 0..1):
  flame         amount (G.ARGS.*_flames.real_intensity, 0..10), colour_1, colour_2, id, t0
                -> use `flame_bake(kind, level)`; kind chips|mult, level 0..4 of FLAME_LEVELS. Size default 120x120 (the sprite
                is 2.5u square, the shader floors to a 60x60 grid). Transparent loop.
  card shaders  layers [PNG bytes|PIL] (center, front...; REQUIRED), extras [flat layers drawn after the effects: seal, soul...],
                effects [...], dissolve (float | (from,to) -> play-once sequence), duration (s, default .7), burn [c1,c2],
                card_id, ambient (ambient_tilt, .2), t0.
                `shader` = card | foil | holo | polychrome | negative | booster | voucher | debuff | played | dissolve
                (the named ones just preset `effects`; `card` takes `effects` itself). Effect order = Card:draw's.
  background    colour_1, colour_2, colour_3 (G.C.BACKGROUND C,L,D), contrast, spin_amount, spin_time -> `bg_bake(name)`
  splash        (splash.fs swirl; the vortex.fs card swirl is a vertex shader, a CSS job) time0, time1, vort_speed, colour_1, colour_2, mid_flash, vort_offset

Looping: Balatro's shader clocks aren't periodic, so loops are N frames + a linear cross-fade over the last K frames
(flame, cards). background is exactly periodic: its two time constants are snapped to one (0.113 -> 0.131121, the sin
term runs 16% faster), the only deviation from the shader.
"""
import hashlib
import io
import json
import math
import os
import time as _time
from pathlib import Path

import numpy as np
from PIL import Image

from .game import DATA_DIR

VERSION = 4
CACHE = Path(os.environ.get("BALATRO_SHADER_CACHE", DATA_DIR / "shaders"))
f64 = np.float64

# ---------------------------------------------------------------- colours (globals.lua)
def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)) + ((int(h[6:8], 16) / 255,) if len(h) == 8 else (1.0,))


def _col(c):
    if isinstance(c, str):
        return _hex(c)
    c = tuple(float(x) for x in c)
    return c if len(c) == 4 else c + (1.0,)


C = {k: _hex(v) for k, v in dict(
    BLUE="009dff", RED="FE5F55", BLACK="374244", ORANGE="fda200", GOLD="eac058", JOKER_GREY="bfc7d5", PURPLE="8867a5",
    FILTER="ff9a00", SPECTRAL="4584fa", SMALL="50846e", WON="4f6367").items()}
C["YELLOW"] = (1.0, 1.0, 0.0, 1.0)
C["CLEAR"] = (0.0, 0.0, 0.0, 0.0)


def _darken(c, p): return tuple(x * (1 - p) for x in c[:3]) + (c[3],)
def _lighten(c, p): return tuple(x * (1 - p) + p for x in c[:3]) + (c[3],)
def _mix(a, b, p): return tuple(a[i] * p + b[i] * (1 - p) for i in range(3)) + (1.0,)


# ---------------------------------------------------------------- GLSL helpers
def _smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def _hsl(r, g, b, a):
    lo, hi = np.minimum(r, np.minimum(g, b)), np.maximum(r, np.maximum(g, b))
    d, s = hi - lo, hi + lo
    l = .5 * s
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = np.where(l < .5, d / s, d / (2 - s))
        h = np.where(hi == r, (g - b) / d, np.where(hi == g, (b - r) / d + 2, (r - g) / d + 4))
    flat = d == 0
    return np.where(flat, 0, np.mod(h / 6, 1)), np.where(flat, 0, sat), l, a


def _hue(s, t, h):
    hs = np.mod(h, 1.) * 6
    return np.where(hs < 1, (t - s) * hs + s, np.where(hs < 3, t, np.where(hs < 4, (t - s) * (4 - hs) + s, s)))


def _rgb(h, s, l, a):
    t = np.where(l < .5, s * l + l, -s * l + (s + l))
    q = 2 * l - t
    grey = s < .0001
    return tuple(np.where(grey, l, _hue(q, t, h + o)) for o in (1 / 3, 0, -1 / 3)) + (a,)


def _field(fu, k, t):
    """The 3-wave `field` of holo/polychrome/dissolve; fu = floored uv already centred+scaled (x, y)."""
    x, y = fu
    p1x, p1y = x + 50 * math.sin(-t / 143.6340), y + 50 * math.cos(-t / 99.4324)
    p2x, p2y = x + 50 * math.cos(t / 53.1532), y + 50 * math.cos(t / 61.4532)
    p3x, p3y = x + 50 * math.sin(-t / 87.53218), y + 50 * math.sin(-t / 49.0)
    return (1. + (np.cos(np.hypot(p1x, p1y) / 19.483) + np.sin(np.hypot(p2x, p2y) / 33.155) * np.cos(p2y / 15.73)
                  + np.cos(np.hypot(p3x, p3y) / 27.193) * np.sin(p3x / 21.92))) / 2.


def _dissolve_mask(tex, uv, px, py, d, time, b1, b2):
    """dissolve_mask() shared by every card shader. tex = (r,g,b,a); d = dissolve uniform; time = per-card `time`."""
    if d < .001:
        return tex
    r, g, b, a = tex
    ad = (d * d * (3. - 2. * d)) * 1.02 - .01
    t = time * 10. + 2003.
    mx = max(px, py)
    fx, fy = np.floor(uv[0] * px) / mx, np.floor(uv[1] * py) / mx  # /max(b,a) as in the shader (borders compare against it)
    field = _field(((fx - .5) * 2.3 * mx, (fy - .5) * 2.3 * mx), 0, t)
    k = 5. + 5. * d
    res = (.5 + .5 * np.cos(ad / 82.612 + (field - .5) * 3.14)
           - np.where(fx > .8, (fx - .8) * k, 0.) * d - np.where(fy > .8, (fy - .8) * k, 0.) * d
           - np.where(fx < .2, (.2 - fx) * k, 0.) * d - np.where(fy < .2, (.2 - fy) * k, 0.) * d)
    if b1[3] > .01:
        edge = (a > .01) & (res < ad + .8 * (.5 - abs(ad - .5))) & (res > ad)
        inner = edge & (res < ad + .5 * (.5 - abs(ad - .5)))
        outer = edge & ~inner
        if b2[3] > .01:
            r, g, b, a = [np.where(outer, b2[i], v) for i, v in enumerate((r, g, b, a))]
        r, g, b, a = [np.where(inner, b1[i], v) for i, v in enumerate((r, g, b, a))]
    return r, g, b, np.where(res > ad, a, 0.)


# ---------------------------------------------------------------- card shaders (tex = (r,g,b,a); u = uniforms dict)
def _fx_dissolve(tex, uv, u):  # dissolve.fs: burn tint, then mask
    d, b1, b2 = u["dissolve"], u["b1"], u["b2"]
    r, g, b, a = tex
    if d > .01:
        c = b2 if b2[3] > .01 else b1 if b1[3] > .01 else None
        if c:
            r, g, b = [v * (1 - .6 * d) + .6 * c[i] * d for i, v in enumerate((r, g, b))]
    return _dissolve_mask((r, g, b, a), uv, u["px"], u["py"], d, u["time"], b1, b2)


def _fx_foil(tex, uv, u):
    r, g, b, a = tex
    fr, fg = u["x"], u["y"]
    low, high = np.minimum(r, np.minimum(g, b)), np.maximum(r, np.maximum(g, b))
    delta = np.minimum(high, np.maximum(.5, 1. - low))
    ax, ay = (uv[0] - .5) * u["px"] / u["py"], uv[1] - .5
    la = np.hypot(90 * ax, 90 * ay)
    fac = np.maximum(np.minimum(2 * np.sin(la + fr * 2. + 3. * (1. + .8 * np.cos(np.hypot(113.1121 * ax, 113.1121 * ay) - fr * 3.121))) - 1. - np.maximum(5. - la, 0.), 1.), 0.)
    rx, ry = math.cos(fr * .1221), math.sin(fr * .3512)
    with np.errstate(divide="ignore", invalid="ignore"):
        angle = (rx * ax + ry * ay) / (math.hypot(rx, ry) * np.hypot(ax, ay))
    fac2 = np.maximum(np.minimum(5. * np.cos(fg * .3 + angle * 3.14 * (2.2 + .9 * math.sin(fr * 1.65 + .2 * fg))) - 4. - np.maximum(2. - np.hypot(20 * ax, 20 * ay), 0.), 1.), 0.)
    fac3 = .3 * np.maximum(np.minimum(2 * np.sin(fr * 5. + uv[0] * 3. + 3. * (1. + .5 * math.cos(fr * 7.))) - 1., 1.), -1.)
    fac4 = .3 * np.maximum(np.minimum(2 * np.sin(fr * 6.66 + uv[1] * 3.8 + 3. * (1. + .5 * math.cos(fr * 3.414))) - 1., 1.), -1.)
    m = np.maximum(np.maximum(fac, np.maximum(fac2, np.maximum(fac3, np.maximum(fac4, 0.)))) + 2.2 * (fac + fac2 + fac3 + fac4), 0.)
    a = np.minimum(a, .3 * a + .9 * np.minimum(.5, m * .1))
    return _dissolve_mask((r - delta + delta * m * .3, g - delta + delta * m * .3, b + delta * m * 1.9, a), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


def _fx_holo(tex, uv, u):
    r, g, b, a = tex
    h, s, l, _ = _hsl(.5 * r, .5 * g, .5 * b + .5, a)
    t = u["y"] * 7.221 + u["time"]
    fu = (np.floor(uv[0] * u["px"]) / u["px"], np.floor(uv[1] * u["py"]) / u["py"])
    field = _field(((fu[0] - .5) * 250., (fu[1] - .5) * 250.), 0, t)
    res = .5 + .5 * np.cos(u["x"] * 2.612 + (field - .5) * 3.14)
    low, high = np.minimum(r, np.minimum(g, b)), np.maximum(r, np.maximum(g, b))
    delta = .2 + .3 * (high - low) + .1 * high
    gs = .79
    ux, uy = uv
    fac = .5 * np.maximum(np.maximum(np.maximum(0., 7. * np.abs(np.cos(ux * gs * 20.)) - 6.), np.maximum(0., 7. * np.cos(uy * gs * 45. + ux * gs * 20.) - 6.)),
                          np.maximum(0., 7. * np.cos(uy * gs * 45. - ux * gs * 20.) - 6.))
    rr, rg, rb, ra = _rgb(h + res + fac, s * 1.3, l * .6 + .4, a)
    out = [(1. - delta) * r + delta * rr * .9, (1. - delta) * g + delta * rg * .8, (1. - delta) * b + delta * rb * 1.2, (1. - delta) * a + delta * ra * a]
    out[3] = np.where(out[3] < .7, out[3] / 3., out[3])
    return _dissolve_mask(tuple(out), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


def _fx_poly(tex, uv, u):
    r, g, b, a = tex
    low, high = np.minimum(r, np.minimum(g, b)), np.maximum(r, np.maximum(g, b))
    sf = 1. - np.maximum(0., .05 * (1.1 - (high - low)))
    h, s, l, _ = _hsl(r * sf, g * sf, b, a)
    t = u["y"] * 2.221 + u["time"]
    fu = (np.floor(uv[0] * u["px"]) / u["px"], np.floor(uv[1] * u["py"]) / u["py"])
    field = _field(((fu[0] - .5) * 50., (fu[1] - .5) * 50.), 0, t)
    res = .5 + .5 * np.cos(u["x"] * 2.612 + (field - .5) * 3.14)
    rr, rg, rb, _ = _rgb(h + res + u["y"] * .04, np.minimum(.6, s + .5), l, a)
    a = np.where(a < .7, a / 3., a)
    return _dissolve_mask((rr, rg, rb, a), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


def _fx_negative(tex, uv, u):
    r, g, b, a = tex
    h, s, l, _ = _hsl(r, g, b, a)
    if u["y"] != 0:
        l = 1. - l
    rr, rg, rb, _ = _rgb(-h + .2, s, l, a)
    a = np.where(a < .7, a / 3., a)
    return _dissolve_mask((rr + .8 * 79 / 255, rg + .8 * 99 / 255, rb + .8 * 103 / 255, a), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


def _shine(tex, uv, u, k):
    """negative_shine / booster / voucher share one structure; k holds each shader's constants."""
    r, g, b, a = tex
    x = u["x"]
    ux, uy = uv
    low, high = np.minimum(r, np.minimum(g, b)), np.maximum(r, np.maximum(g, b))
    delta = k["delta"](low, high)
    fac = .8 + .9 * np.sin(k["f1"][0] * ux + k["f1"][1] * uy + x * 12. + np.cos(x * 5.3 + uy * 4.2 - ux * 4.))
    fac2 = .5 + .5 * np.sin(k["f2"][0] * ux + k["f2"][1] * uy + x * 5. - np.cos(x * 2.3 + ux * 8.2))
    fac3 = .5 + .5 * np.sin(k["f3"][0] * ux + k["f3"][1] * uy + x * 6.111 + np.sin(x * 5.3 + uy * 3.2))
    fac4 = .5 + .5 * np.sin(k["f4"][0] * ux + k["f4"][1] * uy + x * 8.111 + np.sin(x * 1.3 + uy * k["f4y"]))
    fac5 = np.sin(k["f5x"] * ux + 5.32 * uy + x * 12. + np.cos(x * 5.3 + uy * 4.2 - ux * 4.))
    mf = k["mf"] * np.maximum(np.maximum(fac, np.maximum(fac2, np.maximum(fac3, 0.))) + (fac + fac2 + fac3 * fac4), 0.)
    r, g, b = r * .5 + .4, g * .5 + .4, b * .5 + .8
    nr = r - delta + delta * mf * (.7 + fac5 * k["fr"]) - .1
    ng = g - delta + delta * mf * (.7 - fac5 * k["fg"]) - .1
    nb = b - delta + delta * mf * .7 - .1
    na = a * (k["aw"] * np.maximum(np.minimum(1., np.maximum(0., .3 * np.maximum(low * .2, delta) + np.minimum(np.maximum(mf * .1, 0.), .4))), 0.) + .15 * mf * (.1 + delta))
    return _dissolve_mask((nr, ng, nb, na), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


_K_VOUCHER = dict(delta=lambda lo, hi: hi - lo, f1=(13., 5.32), f2=(10., 2.32), f3=(12., 6.32), f4=(4., 2.32), f4y=13.2, f5x=8., mf=.6, fr=.07, fg=.17, aw=.8)
_K_BOOSTER = dict(_K_VOUCHER, delta=lambda lo, hi: np.maximum(hi - lo, lo * .7))
_K_NEGSHINE = dict(delta=lambda lo, hi: hi - lo - .1, f1=(11., 4.32), f2=(8., 2.32), f3=(10., 5.32), f4=(3., 2.32), f4y=11.2, f5x=.9 * 16., mf=.7, fr=.27, fg=.27, aw=.5)
_fx_voucher = lambda t, uv, u: _shine(t, uv, u, _K_VOUCHER)
_fx_booster = lambda t, uv, u: _shine(t, uv, u, _K_BOOSTER)
_fx_negshine = lambda t, uv, u: _shine(t, uv, u, _K_NEGSHINE)


def _fx_debuff(tex, uv, u):
    r, g, b, a = tex
    h, s, l, _ = _hsl(r * .8 + .2, g * .8, b * .8, a)
    w = .1 if u["y"] != 0 else 0.
    x, y = uv
    test = ((x + y > 1 - w) & (x + y < 1 + w)) | (((1 - x) + y > 1 - w) & ((1 - x) + y < 1 + w))
    rr, rg, rb, _ = _rgb(np.where(test, 1., h), np.where(test, .7, .25), np.where(test, .8 * l, .7 * l), a)
    return _dissolve_mask((rr, rg, rb, np.where(test, a, a * .3)), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


def _fx_played(tex, uv, u):
    r, g, b, a = tex
    h, s, l, _ = _hsl(r, g, b, a)
    rr, rg, rb, _ = _rgb(h, s * .5, l * .8, a)
    return _dissolve_mask((rr, rg, rb, a * .5), uv, u["px"], u["py"], u["dissolve"], u["time"], u["b1"], u["b2"])


# Card:draw order. scope: 'c' = center layer only, 'a' = every layer
EFFECTS = {"negative_base": (_fx_negative, "a"), "voucher": (_fx_voucher, "c"), "booster": (_fx_booster, "c"), "holo": (_fx_holo, "a"),
           "foil": (_fx_foil, "a"), "polychrome": (_fx_poly, "a"), "negative": (_fx_negshine, "c"), "debuff": (_fx_debuff, "a"),
           "played": (_fx_played, "a")}
ORDER = ("voucher", "booster", "holo", "foil", "polychrome", "negative", "debuff", "played")
ANIMATED = {"voucher", "booster", "holo", "foil", "polychrome", "negative"}


def _over(canvas, src):
    """love 'alpha' blend onto a premultiplied accumulator; src straight RGBA (clamped like the framebuffer)."""
    r, g, b, a = [np.clip(v, 0., 1.) for v in src]
    ia = 1. - a
    return [r * a + canvas[0] * ia, g * a + canvas[1] * ia, b * a + canvas[2] * ia, a + canvas[3] * ia]


def _layer(im):
    if isinstance(im, (bytes, bytearray)):
        im = Image.open(io.BytesIO(im))
    return np.asarray(im.convert("RGBA"), dtype=f64) / 255.


def _card_frame(L, extras, effects, u):
    h, w = L[0].shape[:2]
    uv = ((np.arange(w) + .5) / w * np.ones((h, 1)), (np.arange(h) + .5)[:, None] / h * np.ones((1, w)))
    cv = [np.zeros((h, w)) for _ in range(4)]
    base = _fx_negative if "negative" in effects else _fx_dissolve
    for im in L:
        cv = _over(cv, base(tuple(im[..., i] for i in range(4)), uv, u))
    for name in ORDER:
        if name in effects:
            fn, scope = EFFECTS[name]
            for im in (L[:1] if scope == "c" else L):
                cv = _over(cv, fn(tuple(im[..., i] for i in range(4)), uv, u))
    for im in extras:
        cv = _over(cv, _fx_dissolve(tuple(im[..., i] for i in range(4)), uv, u))
    return cv  # premultiplied


# ---------------------------------------------------------------- encoding
def _unpremul(cv):
    a = cv[3]
    with np.errstate(divide="ignore", invalid="ignore"):
        rgb = np.where(a[..., None] > 0, np.stack(cv[:3], -1) / np.maximum(a, 1e-9)[..., None], 0.)
    return np.concatenate([np.clip(rgb, 0, 1), a[..., None]], -1)


def _img(cv, size=None):
    im = Image.fromarray((np.clip(_unpremul(cv), 0, 1) * 255 + .5).astype(np.uint8), "RGBA")
    return im.resize(size, Image.NEAREST) if size and tuple(size) != im.size else im


def _encode(ims, ms, loop, lossless=True, quality=80):
    b = io.BytesIO()
    if len(ims) == 1:
        ims[0].save(b, "PNG", optimize=True)
        return b.getvalue(), "image/png"
    ims[0].save(b, "WEBP", save_all=True, append_images=ims[1:], duration=ms, loop=loop, lossless=lossless, quality=quality, method=2 if lossless else 4)
    return b.getvalue(), "image/webp"


def _crossfade(fr, n):
    """fr: n+k premultiplied frames (list of 4 arrays) -> n frames that loop: the last k fade back into the first."""
    k = len(fr) - n
    out = []
    for i in range(n):
        if i < k:
            w = i / k
            out.append([w * fr[i][c] + (1 - w) * fr[n + i][c] for c in range(4)])
        else:
            out.append(fr[i])
    return out


# ---------------------------------------------------------------- flame.fs
FLAME_LEVELS = (1.0, 2.5, 4.5, 7.0, 10.0)
FLAME_IDS = {"chips": 17.0, "mult": 23.0}  # Sprite.ID of the two flame sprites is a runtime counter; any pair is faithful


def _flame_colours(kind):
    base = C["BLUE"] if kind == "chips" else C["RED"]
    acc = tuple(min(max(((base[i] * .5 + C["YELLOW"][i] * .5) + .1) ** 2, .1), 1.) for i in range(3)) + (1.,)  # G.C.UI_CHIPLICK / UI_MULTLICK
    return base, acc


def _flame_frame(n, amount, t, id_, c1, c2):
    """flame.fs on an n x n sprite (uv -0.5..0.5, y down). Returns straight RGBA arrays."""
    intensity = min(10., amount)
    px = (np.arange(n) + .5) / n
    uv = (px[None, :] * np.ones((n, 1)) - .5, px[:, None] * np.ones((1, n)) - .5)
    P = 60.
    fx, fy = np.floor(uv[0] * P) / P, np.floor(uv[1] * P) / P
    wob = 1. + .01 * (np.sin(-1.123 * fx + .2 * t) * np.cos(5.3332 * fy + t * .931))
    fx, fy = fx * wob, fy * wob
    up = np.mod(4. * t, 10000.) - 5000. + np.mod(1.781 * id_, 1000.)
    sf = 7.5 + 3. / (2. + 2. * intensity)
    sx, sy = fx * sf, fy * sf + up
    speed = np.mod(20.781 * id_, 100.) + math.sin(t + id_) * math.cos(t * .151 + id_)
    s2x = np.zeros_like(sx)
    s2y = np.zeros_like(sx)
    for i in range(5):
        ln = np.hypot(sx, sy)
        k = .3 * (np.cos(ln * .411) + .3344 * np.sin(ln) - .23 * np.cos(ln))
        sg = -1. if (i % 2) > 1 else 1.  # mod(float(i),2.)>1. is never true: kept verbatim
        s2x, s2y = s2x + sx + .05 * s2y * sg + k, s2y + sy + .05 * s2x * sg + k  # sv2 += sv + .05*sv2.yx*sg + k
        sx, sy = sx + .5 * np.cos(np.cos(s2y) + speed * .0812) * np.sin(3.22 + s2x - speed * .1531), \
            sy + .5 * np.sin(-s2x * 1.21222 + .113785 * speed) * np.cos(s2y * .91213 - .13582 * speed)
    smoke = np.maximum(0., (np.hypot((sx) / sf * 5., (sy - up) / sf * 5.) + .1 * (np.hypot(fx, fy) - .5)) * (2. / (2. + intensity * .2)))
    smoke = smoke + max(0., 2. - .3 * intensity) * np.maximum(0., 2. * (fy - .5) * (fy - .5))
    smoke = np.where(np.abs(uv[0]) > .4, smoke + 10. * (np.abs(uv[0]) - .4), smoke)
    ell = np.hypot((uv[0]) * .19, (uv[1] - .1))
    smoke = np.where((ell < min(.1, intensity * .5)) & (smoke > 1.), smoke + min(8.5, intensity * 10.) * (ell - .1), smoke)
    col = np.broadcast_to(np.array(c1[:3]), (n, n, 3)).copy()
    c2 = np.array(c2[:3])
    dy = .12 - uv[1]
    hot = (uv[1] < .12)[..., None]
    shaded = col * (1. - .5 * dy[..., None]) + 2.5 * dy[..., None] * c2
    shaded = shaded + shaded * ((-2. + .5 * intensity * smoke) * dy)[..., None]
    col = np.where(hot, shaded, col)
    a = np.where(smoke > 1., 0., 1.)
    return col, a


def _bake_flame(p, size, frames, fps):
    n = (size or (120, 120))[0]
    amount = float(p.get("amount", 5))
    c1, c2 = (_col(p["colour_1"]), _col(p["colour_2"])) if "colour_1" in p else _flame_colours(p.get("kind", "chips"))
    id_ = float(p.get("id", FLAME_IDS.get(p.get("kind", "chips"), 17.)))
    fps = fps or 24
    N = frames or 72
    K = max(4, N // 12)
    sp = 1. + .2 * min(10., amount)  # _F.timer += dt*(1 + intensity*0.2)
    fr = []
    for i in range(N + K):
        col, a = _flame_frame(n, amount, float(p.get("t0", 100.)) + i / fps * sp, id_, c1, c2)
        fr.append([col[..., 0] * a, col[..., 1] * a, col[..., 2] * a, a])
    return [_img(x) for x in _crossfade(fr, N)], round(1000 / fps), 0, True


# ---------------------------------------------------------------- cards
PRESETS = {"foil": ["foil"], "holo": ["holo"], "polychrome": ["polychrome"], "negative": ["negative"], "booster": ["booster"],
           "voucher": ["voucher"], "debuff": ["debuff"], "played": ["played"], "dissolve": [], "card": None}


def _bake_card(shader, p, size, frames, fps):
    L = [_layer(x) for x in p["layers"]]
    extras = [_layer(x) for x in p.get("extras", ())]
    effects = set(p.get("effects") if PRESETS[shader] is None else PRESETS[shader])
    h, w = L[0].shape[:2]
    dis = p.get("dissolve", 0.)
    burn = [_col(x) for x in p.get("burn", [])] + [C["CLEAR"]] * 2
    anim = bool(effects & ANIMATED)
    u = dict(px=p.get("px", 71), py=p.get("py", 95), time=123.33412 * (float(p.get("card_id", 1.)) / 1.14212) % 3000, b1=burn[0], b2=burn[1])
    fps = fps or (30 if isinstance(dis, (list, tuple)) else 12)
    t0, amb = float(p.get("t0", 40.)), float(p.get("ambient", .2))

    def frame(i, period, d):
        T = t0 + i / fps
        a = 2 * math.pi * i / period
        u.update(dissolve=d, x=T / 28 + amb * (.5 + math.cos(a)) * .3, y=T)  # Card:draw send_to_shader
        return _card_frame(L, extras, effects, u)

    if isinstance(dis, (list, tuple)):  # play-once dissolve/materialize sequence
        d0, d1 = dis
        N = frames or max(2, round(float(p.get("duration", .7)) * fps) + 1)
        fr = [frame(i, 60., d0 + (d1 - d0) * i / (N - 1)) for i in range(N)]
        return [_img(x, size) for x in fr], round(1000 / fps), 1, True
    if not anim:
        return [_img(frame(0, 60., float(dis)), size)], 0, 0, True
    N = frames or 48
    K = N // 4
    fr = [frame(i, N, float(dis)) for i in range(N + K)]
    return [_img(x, size) for x in _crossfade(fr, N)], round(1000 / fps), 0, True


# ---------------------------------------------------------------- background.fs / splash.fs
BG_PERIOD = 2 * math.pi / .262242  # 23.96 s: cos(.131121*speed) with speed = 2t


def _bg_frame(w, h, t, c1, c2, c3, contrast, spin_amount, spin_time):
    S = np.array([w, h], f64)
    ln = float(np.hypot(w, h))
    ps = ln / 700.
    x = (np.arange(w) + .5)[None, :] * np.ones((h, 1))
    y = (np.arange(h) + .5)[:, None] * np.ones((1, w))
    ux = (np.floor(x / ps) * ps - .5 * S[0]) / ln - .12
    uy = (np.floor(y / ps) * ps - .5 * S[1]) / ln
    ul = np.hypot(ux, uy)
    speed = spin_time * .5 * .2 + 302.2
    ang = np.arctan2(uy, ux) + speed - .5 * 20. * (spin_amount * ul + (1. - spin_amount))
    mx, my = S[0] / ln / 2., S[1] / ln / 2.
    ux, uy = ul * np.cos(ang) + mx - mx, ul * np.sin(ang) + my - my
    ux, uy = ux * 30., uy * 30.
    sp = t * 2.
    u2x = u2y = ux + uy
    for _ in range(5):
        k = np.sin(np.maximum(ux, uy))
        u2x, u2y = u2x + k + ux, u2y + k + uy
        ux, uy = ux + .5 * np.cos(5.1123314 + .353 * u2y + sp * .131121), uy + .5 * np.sin(u2x - .131121 * sp)  # .113 snapped
        k2 = np.cos(ux + uy) - np.sin(ux * .711 - uy)
        ux, uy = ux - k2, uy - k2
    cm = .25 * contrast + .5 * spin_amount + 1.2
    pr = np.minimum(2., np.maximum(0., np.hypot(ux, uy) * .035 * cm))
    c1p = np.maximum(0., 1. - cm * np.abs(1. - pr))
    c2p = np.maximum(0., 1. - cm * np.abs(pr))
    c3p = 1. - np.minimum(1., c1p + c2p)
    k = .3 / contrast
    out = [k * c1[i] + (1 - k) * (c1[i] * c1p + c2[i] * c2p + c3p * (c3[i] if i < 3 else c1[3])) for i in range(4)]
    return [np.clip(v, 0, 1) for v in out]


def _bake_bg(p, size, frames, fps):
    w, h = size or (427, 240)  # background.fs's grid is 700 cells along the diagonal; app bakes 0.7x of it (one px per cell) so ~5.8 fps fits
    c1, c2, c3 = (_col(p.get(k, d)) for k, d in (("colour_1", "374244"), ("colour_2", "ffff00"), ("colour_3", "374244")))
    N = frames or min(192, (56 << 20) // (w * h * 4))  # under Tern's 64 MiB decoded / 1024-frame ceiling
    ims = []
    for i in range(N):
        r, g, b, _ = _bg_frame(w, h, i * BG_PERIOD / N, c1, c2, c3, float(p.get("contrast", 1.)), float(p.get("spin_amount", 0.)), float(p.get("spin_time", 0.)))
        ims.append(Image.fromarray((np.stack([r, g, b], -1) * 255 + .5).astype(np.uint8), "RGB"))
    return ims, round(BG_PERIOD / N * 1000), 0, False


def _bake_splash(p, size, frames, fps):
    w, h = size or (256, 144)
    c1, c2 = _col(p.get("colour_1", "fe5f55")), _col(p.get("colour_2", "009dff"))
    vs, off, mid = float(p.get("vort_speed", .4)), float(p.get("vort_offset", 0)), float(p.get("mid_flash", 0))
    t0, t1 = float(p.get("time0", 0)), float(p.get("time1", 10))
    fps = fps or 12
    N = frames or max(2, round((t1 - t0) * fps))
    S, ln = (w, h), math.hypot(w, h)
    ps = ln / 700.
    x, y = (np.arange(w) + .5)[None, :] * np.ones((h, 1)), (np.arange(h) + .5)[:, None] * np.ones((1, w))
    ux, uy = (np.floor(x / ps) * ps - .5 * w) / ln, (np.floor(y / ps) * ps - .5 * h) / ln
    ul = np.hypot(ux, uy)
    ims = []
    for i in range(N):
        t = t0 + (t1 - t0) * i / (N - 1)
        spd = t * vs
        ang = np.arctan2(uy, ux) + (2.2 + .4 * min(6., spd)) * ul - 1. - spd * .05 - min(6., spd) * spd * .02 + off
        mx, my = w / ln / 2., h / ln / 2.
        sx, sy = (ul * np.cos(ang) + mx - mx) * 30., (ul * np.sin(ang) + my - my) * 30.
        spd = t * 6. * vs + off + 1033.
        u2x = u2y = sx + sy
        for _ in range(5):
            k = np.sin(np.maximum(sx, sy))
            u2x, u2y = u2x + k + sx, u2y + k + sy
            sx, sy = sx + .5 * np.cos(5.1123314 + .353 * u2y + spd * .131121), sy + .5 * np.sin(u2x - .113 * spd)
            k2 = np.cos(sx + sy) - np.sin(sx * .711 - sy)
            sx, sy = sx - k2, sy - k2
        sm = np.minimum(2., np.maximum(-2., 1.5 + np.hypot(sx, sy) * .12 - .17 * min(10., t * 1.2 - 4.)))
        sm = np.where(sm < .2, (sm - .2) * .6 + .2, sm)
        c1p, c2p = np.maximum(0., 1. - 2. * np.abs(1. - sm)), np.maximum(0., 1. - 2. * sm)
        cb = 1. - np.minimum(1., c1p + c2p)
        blk = (.6 * 79 / 255, .6 * 99 / 255, .6 * 103 / 255)
        mf = np.maximum(mid * .8, np.maximum(c1p, c2p) * 5. - 4.4) + mid * np.maximum(c1p, c2p)
        rgb = [(c1[j] * c1p + c2[j] * c2p + cb * blk[j]) * (1. - mf) + mf for j in range(3)]
        ims.append(Image.fromarray((np.clip(np.stack(rgb, -1), 0, 1) * 255 + .5).astype(np.uint8), "RGB"))
    return ims, round(1000 / fps), 1, False


SHADERS = ("flame", "background", "splash") + tuple(PRESETS)


# ---------------------------------------------------------------- public API
def _norm(o):
    if isinstance(o, (bytes, bytearray)):
        return "sha1:" + hashlib.sha1(o).hexdigest()
    if isinstance(o, Image.Image):
        return "sha1:" + hashlib.sha1(o.convert("RGBA").tobytes() + str(o.size).encode()).hexdigest()
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in sorted(o.items())}
    if isinstance(o, (list, tuple)):
        return [_norm(v) for v in o]
    return o


def bake(shader, params=None, size=None, frames=None, fps=None):
    """(key, bytes, mime). Cached on disk; same arguments -> same bytes."""
    p = params or {}
    if shader not in SHADERS:
        raise ValueError(f"unknown shader {shader!r}; one of {SHADERS}")
    key = hashlib.sha1(json.dumps([VERSION, shader, _norm(p), size, frames, fps], sort_keys=True, default=str).encode()).hexdigest()[:24]
    for ext, mime in (("webp", "image/webp"), ("png", "image/png")):
        f = CACHE / f"{shader}-{key}.{ext}"
        if f.exists():
            return key, f.read_bytes(), mime
    if shader == "flame":
        ims, ms, loop, lossless = _bake_flame(p, size, frames, fps)
    elif shader == "background":
        ims, ms, loop, lossless = _bake_bg(p, size, frames, fps)
    elif shader == "splash":
        ims, ms, loop, lossless = _bake_splash(p, size, frames, fps)
    else:
        ims, ms, loop, lossless = _bake_card(shader, p, size, frames, fps)
    w, h = ims[0].size
    assert len(ims) <= 1024 and w * h * 4 * len(ims) <= 64 << 20, "over Tern's animated image limits (1024 frames / 64 MiB decoded)"
    data, mime = _encode(ims, ms, loop, lossless, quality=82)
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f".{os.getpid()}-{key}"
    tmp.write_bytes(data)
    tmp.replace(CACHE / f"{shader}-{key}.{'webp' if mime == 'image/webp' else 'png'}")
    return key, data, mime


# --- conveniences for Motion / Scene
def flame_level(real_intensity):
    """Index into FLAME_LEVELS for G.ARGS.*_flames.real_intensity (None when the shader draws nothing: < 0.1)."""
    if real_intensity < .1:
        return None
    return min(range(len(FLAME_LEVELS)), key=lambda i: abs(FLAME_LEVELS[i] - min(10., real_intensity)))


def flame_bake(kind, level):
    """kind 'chips'|'mult'; level index of FLAME_LEVELS."""
    return bake("flame", {"kind": kind, "amount": FLAME_LEVELS[level]})


def bg_set(name, boss=None):
    """Final shader uniforms (colour_1..3, contrast, spin_amount) of ease_background_colour_blind for a state.
    name: small|big|boss|showdown|won|shop (= small, the blind name is '' in the shop)|tarot|spectral|standard|buffoon|planet; boss = '#hex'."""
    def std(new, sp=None, ter=None, contrast=1.):
        if sp and ter:
            return new, sp, ter, contrast  # L, C, D
        d = .4 if sp else .7
        return tuple(x * 1.3 for x in new[:3]), sp or tuple(x * .9 for x in new[:3]), tuple(x * d for x in new[:3]), contrast
    blk = C["BLACK"]
    if name in ("small", "big", "shop"):
        L, Cc, D, k = std(C["SMALL"])
    elif name == "won":
        L, Cc, D, k = std(C["WON"])
    elif name == "tarot":
        L, Cc, D, k = std(C["PURPLE"], _darken(blk, .2), None, 1.5)
    elif name == "spectral":
        L, Cc, D, k = std(C["SPECTRAL"], _darken(blk, .2), None, 2.)
    elif name == "standard":
        L, Cc, D, k = std(_darken(blk, .2), C["RED"], None, 3.)
    elif name == "buffoon":
        L, Cc, D, k = std(C["FILTER"], blk, None, 2.)
    elif name == "planet":
        L, Cc, D, k = std(blk, None, None, 3.)
    elif name == "showdown":
        L, Cc, D, k = std(C["BLUE"], C["RED"], _darken(blk, .4), 3.)
    else:  # boss
        bc = _col(boss or "b44430")
        L, Cc, D, k = std(_lighten(_mix(bc, blk, .3), .1), bc, None, 2.)
    f = lambda c: tuple(round(x, 5) for x in c[:3])
    spin = {"boss": .25, "showdown": .5}.get(name, 0.)  # Blind:set_blind -> G.ARGS.spin.real (eased amount at rest)
    return dict(colour_1=f(Cc), colour_2=f(L), colour_3=f(D), contrast=k, spin_amount=spin)


def bg_bake(name, boss=None, **kw):
    return bake("background", bg_set(name, boss), **kw)


def bg_tiles(name, boss, size, fps=15, cached_only=False):
    """background.fs at its own grid (size = cells, one px each), cut into a cols x rows mosaic of looping WebPs so
    every tile stays under Tern's per-image ceiling (1024 frames / 64 MiB decoded) at `fps`.  Tern starts an
    animation when the image first draws, on the window's clock: tiles mounted in one render stay in step.
    Lossless: the swirl is a few hundred mixes of three colours, and a lossy encoder (q82 was ~2 KiB a frame) smears
    its one-cell detail into blocky blur once Tern scales the cells up.  The frames share one palette of <= 256 of
    their own colours (median cut of a sample; at most a few levels off where a state has more), so WebP's lossless
    palette mode keeps them at ~20-45 KiB a frame.
    -> [(key, bytes, mime, x0, y0, x1, y1)] with the tile's cell rect.  Cached on disk like bake(); `cached_only`: None
    instead of baking when a tile file is missing."""
    w, h = size
    p = bg_set(name, boss)
    N = min(1024, round(BG_PERIOD * fps))
    budget = (60 << 20) // (N * 4)  # px per tile
    cols = rows = 1
    while -(-w // cols) * -(-h // rows) > budget:
        if w / cols >= h / rows:
            cols += 1
        else:
            rows += 1
    xs = [w * i // cols for i in range(cols + 1)]
    ys = [h * j // rows for j in range(rows + 1)]
    rects = [(xs[i], ys[j], xs[i + 1], ys[j + 1]) for j in range(rows) for i in range(cols)]
    key = hashlib.sha1(json.dumps([VERSION, "bgtiles-pal", _norm(p), size, N], sort_keys=True, default=str).encode()).hexdigest()[:24]
    files = [CACHE / f"bgt-{key}-{n}.webp" for n in range(len(rects))]
    if cached_only and not all(f.exists() for f in files):
        return None
    if not all(f.exists() for f in files):
        c1, c2, c3 = (_col(p[k]) for k in ("colour_1", "colour_2", "colour_3"))
        frames = []
        for i in range(N):
            r, g, b, _ = _bg_frame(w, h, i * BG_PERIOD / N, c1, c2, c3, float(p.get("contrast", 1.)), float(p.get("spin_amount", 0.)), 0.)
            frames.append((np.stack([r, g, b], -1) * 255 + .5).astype(np.uint8))
        pal = Image.fromarray(np.concatenate(frames[::max(1, N // 32)], 0), "RGB").quantize(256, method=Image.Quantize.MEDIANCUT,
                                                                                              dither=Image.Dither.NONE)
        frames = [np.asarray(Image.fromarray(fr, "RGB").quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")) for fr in frames]
        CACHE.mkdir(parents=True, exist_ok=True)
        for f, (x0, y0, x1, y1) in zip(files, rects):
            data, _ = _encode([Image.fromarray(fr[y0:y1, x0:x1], "RGB") for fr in frames], round(BG_PERIOD / N * 1000), 0, True, quality=50)
            tmp = f.with_name(f".{os.getpid()}-{f.name}")
            tmp.write_bytes(data)
            tmp.replace(f)
    return [(f"{key}-{n}", f.read_bytes(), "image/webp", *r) for n, (f, r) in enumerate(zip(files, rects))]


def card_effects(card):
    """Effects for a snapshot card dict (edition str, debuff flag, facing)."""
    ed = card.get("edition")
    ef = [ed] if ed in ("foil", "holo", "polychrome", "negative") else []
    if card.get("debuff"):
        ef.append("debuff")
    return ef


def face_layers(card):
    """(layers, extras) PNG bytes for a snapshot card, from the same atlas cells assets.card_png composites."""
    from . import assets as A
    xy = lambda p: (p["x"], p["y"]) if p else None
    cell = lambda atlas, pos: A._png(A._cell(atlas, *pos))
    layers = [cell(A._center_atlas(card), xy(card["pos"]))]
    if xy(card.get("front_pos")) and card["key"] != "m_stone":
        layers.append(cell(card.get("front_atlas") or "cards_1", xy(card["front_pos"])))
    extras = [cell(A._center_atlas(card), xy(card["soul_pos"]))] if card.get("soul_pos") else []
    if card.get("seal") in A.SEALS:
        extras.append(cell("centers", A.SEALS[card["seal"]]))
    return layers, extras


def card_bake(layers, effects=(), **kw):
    """Composited face with Card:draw's effect stack. layers = [center, front] PNG bytes. kw: dissolve, burn, card_id, extras, size, fps."""
    size, fps, frames = kw.pop("size", None), kw.pop("fps", None), kw.pop("frames", None)
    return bake("card", dict(layers=list(layers), effects=list(effects), **kw), size, frames, fps)


def snapshot_card_bake(card, **kw):
    """card_bake for a snapshot card dict: its layers/extras and card_effects(card). Plain cards with no effect give a PNG."""
    layers, extras = face_layers(card)
    return card_bake(layers, card_effects(card), extras=extras, **kw)


if __name__ == "__main__":  # smoke check: every shader bakes, loops are sane, sizes/times printed
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from balatro_tern import assets
    a = lambda atlas, x, y: assets._png(assets._cell(atlas, x, y))
    face = [a("centers", 1, 0), a("cards_1", 12, 3)]
    joker = [a("Joker", 0, 0)]
    jobs = [("flame", {"kind": "chips", "amount": 5}), ("flame", {"kind": "mult", "amount": 10}),
            ("background", bg_set("small")), ("splash", {"time1": 3})]
    jobs += [(e, {"layers": face}) for e in ("foil", "holo", "polychrome", "negative", "debuff", "played")]
    jobs += [("voucher", {"layers": joker}), ("booster", {"layers": joker}),
             ("dissolve", {"layers": face, "dissolve": (0, 1), "burn": ["374244", "fda200"]})]
    for sh, p in jobs:
        t = _time.perf_counter()
        k, d, m = bake(sh, p)
        n = getattr(Image.open(io.BytesIO(d)), "n_frames", 1)
        print(f"{sh:11s} {k} {m:10s} {len(d) / 1024:8.1f} KiB {n:4d} frames {_time.perf_counter() - t:6.2f}s")
