"""Колода (78 карт из папки taro_cards) и сборка картинки расклада.

Картинка рисуется локально через Pillow и отправляется одним фото,
которое редактируется по мере выбора карт — так весь расклад живёт
в одном сообщении, а не в пачке отдельных фотографий.
"""
from __future__ import annotations

import io
import random
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / "taro_cards"
BACK_PATH = ROOT / "Card_back.png"

CARD_W, CARD_H = 250, 452
GAP = 26
PAD_X, PAD_TOP, PAD_BOTTOM = 44, 40, 64
BG_TOP = (18, 14, 38)
BG_BOTTOM = (44, 26, 72)
GOLD = (226, 190, 120)

NUM_MAP = {
    1: "Туз", 2: "Двойка", 3: "Тройка", 4: "Четвёрка", 5: "Пятёрка",
    6: "Шестёрка", 7: "Семёрка", 8: "Восьмёрка", 9: "Девятка", 10: "Десятка",
    11: "Паж", 12: "Рыцарь", 13: "Королева", 14: "Король",
}
SUIT_MAP = {"Wands": "Жезлов", "Cups": "Кубков", "Swords": "Мечей", "Pentacles": "Пентаклей"}
MAJOR_MAP = {
    "The Fool": "Шут", "The Magician": "Маг", "The High Priestess": "Верховная Жрица",
    "The Empress": "Императрица", "The Emperor": "Император", "The Hierophant": "Иерофант",
    "The Lovers": "Влюблённые", "The Chariot": "Колесница", "Strength": "Сила",
    "The Hermit": "Отшельник", "Wheel of Fortune": "Колесо Фортуны", "Justice": "Справедливость",
    "The Hanged Man": "Повешенный", "Death": "Смерть", "Temperance": "Умеренность",
    "The Devil": "Дьявол", "The Tower": "Башня", "The Star": "Звезда", "The Moon": "Луна",
    "The Sun": "Солнце", "Judgement": "Суд", "The World": "Мир",
}

# Позиции раскладов: их получает и пользователь (в подписи), и модель.
SPREADS = {
    1: {"title": "Совет карты", "cost": 1, "positions": ["Совет"]},
    3: {"title": "Три карты", "cost": 2, "positions": ["Прошлое", "Настоящее", "Будущее"]},
    5: {
        "title": "Большой расклад",
        "cost": 3,
        "positions": ["Ситуация", "Препятствие", "Совет", "Ресурс", "Итог"],
    },
}
REVERSED_CHANCE = 0.3


def _card_from_file(fn: Path) -> dict:
    stem = fn.stem
    cid = stem.lower().replace(" ", "_")
    m = re.match(r"^(\d+)\s+(\w+)$", stem)
    if m:
        name = f"{NUM_MAP.get(int(m.group(1)), m.group(1))} {SUIT_MAP.get(m.group(2), m.group(2))}"
        major = False
    else:
        name = MAJOR_MAP.get(stem, stem)
        major = True
    return {"id": cid, "name": name, "major": major, "path": str(fn)}


@lru_cache(maxsize=1)
def all_cards() -> tuple[dict, ...]:
    files = sorted(p for p in CARDS_DIR.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    return tuple(_card_from_file(p) for p in files)


def card_by_id(cid: str) -> dict | None:
    return next((c for c in all_cards() if c["id"] == cid), None)


def shuffled_deck(rnd: random.Random | None = None) -> list[str]:
    rnd = rnd or random.SystemRandom()
    ids = [c["id"] for c in all_cards()]
    rnd.shuffle(ids)
    return ids


def draw_reversed(rnd: random.Random | None = None) -> bool:
    rnd = rnd or random.SystemRandom()
    return rnd.random() < REVERSED_CHANCE


def daily_pick(uid: int, day_iso: str) -> tuple[str, bool]:
    """Карта дня одинакова весь день для одного пользователя."""
    rnd = random.Random(f"daily:{uid}:{day_iso}")
    ids = [c["id"] for c in all_cards()]
    return rnd.choice(ids), rnd.random() < REVERSED_CHANCE


# ── картинки ─────────────────────────────────────────
@lru_cache(maxsize=128)
def _card_img(cid: str, reversed_: bool) -> Image.Image:
    card = card_by_id(cid)
    im = Image.open(card["path"]).convert("RGB").resize((CARD_W, CARD_H), Image.LANCZOS)
    return im.rotate(180) if reversed_ else im


@lru_cache(maxsize=1)
def _back_img() -> Image.Image:
    im = Image.open(BACK_PATH).convert("RGB")
    # Обрезаем по центру под пропорции карт, потом масштабируем.
    target = CARD_W / CARD_H
    w, h = im.size
    if w / h > target:
        nw = int(h * target)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / target)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    return im.resize((CARD_W, CARD_H), Image.LANCZOS)


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _gradient(w: int, h: int) -> Image.Image:
    top = Image.new("RGB", (w, h), BG_TOP)
    bottom = Image.new("RGB", (w, h), BG_BOTTOM)
    mask = Image.linear_gradient("L").resize((w, h))
    return Image.composite(bottom, top, mask)


def render_spread(slots: list[tuple[str, bool] | None], highlight: int | None = None) -> bytes:
    """slots: список (card_id, reversed) или None для ещё закрытой позиции."""
    n = max(1, len(slots))
    w = PAD_X * 2 + n * CARD_W + (n - 1) * GAP
    w = max(w, 640)
    h = PAD_TOP + CARD_H + PAD_BOTTOM
    canvas = _gradient(w, h)
    draw = ImageDraw.Draw(canvas)
    x0 = (w - (n * CARD_W + (n - 1) * GAP)) // 2
    font = _font(26)
    for i, slot in enumerate(slots):
        x = x0 + i * (CARD_W + GAP)
        img = _card_img(*slot) if slot else _back_img()
        if highlight == i:
            draw.rounded_rectangle(
                (x - 7, PAD_TOP - 7, x + CARD_W + 7, PAD_TOP + CARD_H + 7), radius=14, outline=GOLD, width=4
            )
        canvas.paste(img, (x, PAD_TOP))
        if n > 1:
            cx, cy = x + CARD_W // 2, PAD_TOP + CARD_H + PAD_BOTTOM // 2
            r = 19
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=GOLD, width=3)
            draw.text((cx, cy), str(i + 1), fill=GOLD, font=font, anchor="mm")
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def render_single(cid: str, reversed_: bool) -> bytes:
    return render_spread([(cid, reversed_)])


def card_label(cid: str, reversed_: bool) -> str:
    card = card_by_id(cid)
    name = card["name"] if card else cid
    return f"{name} (перевёрнутая)" if reversed_ else name
