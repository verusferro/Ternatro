"""Atlas crops -> PNG bytes. Uses extracted/resources/textures/2x (cells are 2x: 142x190 cards, 68x68 blinds/tags)."""
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image

TEX = Path(__file__).resolve().parent.parent / "extracted/resources/textures/2x"
# G.ASSET_ATLAS name -> (file, cell w, cell h) at 2x (game.lua asset_atli / animation_atli)
ATLAS = {
    "cards_1": ("8BitDeck.png", 142, 190), "cards_2": ("8BitDeck_opt2.png", 142, 190),
    "centers": ("Enhancers.png", 142, 190), "Joker": ("Jokers.png", 142, 190),
    "Tarot": ("Tarots.png", 142, 190), "Planet": ("Tarots.png", 142, 190), "Spectral": ("Tarots.png", 142, 190),
    "Voucher": ("Vouchers.png", 142, 190), "Booster": ("boosters.png", 142, 190),
    "stickers": ("stickers.png", 142, 190), "tags": ("tags.png", 68, 68),
    "blind_chips": ("BlindChips.png", 68, 68), "chips": ("chips.png", 58, 58), "icons": ("icons.png", 132, 132),
}
SEALS = {"Gold": (2, 0), "Purple": (4, 4), "Red": (5, 4), "Blue": (6, 4)}  # G.shared_seals (Enhancers)
BACK_POS = {"b_red": (0, 0)}  # default deck back; others via card["pos"] when key startswith "b_"


@lru_cache(maxsize=None)
def _sheet(atlas: str) -> Image.Image:
    return Image.open(TEX / ATLAS[atlas][0]).convert("RGBA")


@lru_cache(maxsize=None)
def _cell(atlas: str, x: int, y: int) -> Image.Image:
    _, w, h = ATLAS[atlas]
    return _sheet(atlas).crop((x * w, y * h, (x + 1) * w, (y + 1) * h))


def _png(img: Image.Image) -> bytes:
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


@lru_cache(maxsize=None)
def sprite_png(atlas: str, x: int, y: int) -> tuple[str, bytes]:
    return f"{atlas}:{x},{y}", _png(_cell(atlas, x, y))


def _center_atlas(card: dict) -> str:
    a = card.get("atlas")
    if a in ATLAS:
        return a
    return card["set"] if card["set"] in ATLAS and card["set"] != "Default" else "centers"


@lru_cache(maxsize=None)
def _card(key: str, atlas: str, pos: tuple, soul, front_atlas, front_pos, seal, back: bool) -> bytes:
    img = _cell(atlas, *pos).copy()
    if not back:
        if front_pos is not None and key != "m_stone":  # stone hides the rank/suit front
            img.alpha_composite(_cell(front_atlas, *front_pos))
        if soul is not None:
            img.alpha_composite(_cell(atlas, *soul))
        if seal in SEALS:
            img.alpha_composite(_cell("centers", *SEALS[seal]))
    return _png(img)


def card_png(card: dict) -> tuple[str, bytes]:
    """(cache key, PNG) for a snapshot card; back if facing == 'back'."""
    xy = lambda p: (p["x"], p["y"]) if p else None
    back = card.get("facing") == "back"
    if back:  # the card's deck (snapshot back_pos), else Red deck
        pos = xy(card.get("back_pos")) or BACK_POS["b_red"]
        atlas, key, soul, fa, fp, seal = "centers", "back", None, None, None, None
    else:
        key, atlas, pos = card["key"], _center_atlas(card), xy(card["pos"])
        soul, fa, fp, seal = xy(card.get("soul_pos")), card.get("front_atlas") or "cards_1", xy(card.get("front_pos")), card.get("seal")
    png = _card(key, atlas, pos, soul, fa, fp, seal, back)
    ck = f"{key}|{atlas}|{pos}|{soul}|{fa}|{fp}|{seal}"
    return ck, png


if __name__ == "__main__":
    S = lambda x, y: {"x": x, "y": y}
    cards = [
        dict(key="c_base", set="Default", atlas="centers", pos=S(1, 0), front_atlas="cards_1", front_pos=S(12, 3)),  # SA?
        dict(key="c_base", set="Default", atlas="centers", pos=S(1, 0), front_atlas="cards_1", front_pos=S(10, 0)),
        dict(key="m_glass", set="Enhanced", atlas="centers", pos=S(5, 2), front_atlas="cards_1", front_pos=S(11, 2), seal="Red"),
        dict(key="m_stone", set="Enhanced", atlas="centers", pos=S(5, 0), front_atlas="cards_1", front_pos=S(3, 1)),
        dict(key="m_gold", set="Enhanced", atlas="centers", pos=S(6, 0), front_atlas="cards_1", front_pos=S(0, 0), seal="Gold"),
        dict(key="j_joker", set="Joker", atlas="Joker", pos=S(0, 0)),
        dict(key="j_perkeo", set="Joker", atlas="Joker", pos=S(7, 8), soul_pos=S(7, 9)),
        dict(key="c_fool", set="Tarot", atlas="Tarot", pos=S(0, 0)),
        dict(key="c_pluto", set="Planet", atlas="Tarot", pos=S(0, 3)),
        dict(key="v_overstock_norm", set="Voucher", atlas="Voucher", pos=S(0, 0)),
        dict(key="p_arcana_normal_1", set="Booster", atlas="Booster", pos=S(0, 0)),
        dict(key="c_base", set="Default", facing="back"),
    ]
    sheet = Image.new("RGBA", (142 * 6 + 70, 190 * 2 + 20), (30, 90, 60, 255))
    for i, c in enumerate(cards):
        k, p = card_png(c)
        sheet.alpha_composite(Image.open(io.BytesIO(p)), (10 + (i % 6) * 142 + (i % 6) * 10, 10 + (i // 6) * 200))
    sheet.save("/tmp/contact.png")
    assert card_png(cards[0])[0] != card_png(cards[1])[0]
    assert sprite_png("blind_chips", 0, 0)[1][:4] == b"\x89PNG"
    print("ok /tmp/contact.png")
