# bot.py — Таро и нумерология.
# Принцип интерфейса: у пользователя всегда ОДНО активное сообщение с кнопками
# («экран»). Меню и промежуточные шаги редактируются на месте; готовые
# разборы остаются в истории без кнопок, а новый экран появляется под ними.
# Никаких фоновых напоминаний: бот пишет только в ответ на действие.
# -*- coding: utf-8 -*-
import asyncio
import contextlib
import datetime as dt
import html
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    CopyTextButton,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Mk,
    InputMediaPhoto,
    LabeledPrice,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    JobQueue,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest

import db
from db import (
    add_alias,
    add_credits,
    alias_owner,
    aliases_for,
    all_user_ids,
    claim_ref_reward,
    count_new_users,
    daily_stats,
    event_stats,
    get_credits,
    get_daily,
    get_profile,
    get_screen,
    get_subscribed,
    is_new_user,
    log_ab_event,
    log_event,
    log_payment,
    payment_rows,
    referral_count,
    referral_counts,
    referral_payment_rows,
    refund_credits,
    register_user,
    save_daily,
    set_profile,
    set_referral,
    set_screen,
    set_subscribed,
    spend_credits,
    unique_event_users,
    unique_event_users_like,
    unique_payers,
)
from utils import deck, numerology
from utils.llm import build_daily, build_numerology, build_tarot, llm_queue_size

db.init_sub()

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
START_TIME = dt.datetime.now()

# ── ENV ──────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YK_TOKEN = os.getenv("YK_TOKEN")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/obotizatciaSupp_bot")
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip()}
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002883355776") or 0)
JOIN_URL = os.getenv("JOIN_URL", "https://t.me/obotization/3")
SUB_CACHE_SEC = 15 * 60

# ── Экономика ────────────────────────────────────────
# Пакеты: баллы → цена в копейках и в звёздах (≈ 1.8 ₽ за ⭐ для покупателя).
PACKS = {
    6: {"rub": 99_00, "stars": 55, "tag": ""},
    15: {"rub": 199_00, "stars": 110, "tag": " 🔥"},
    40: {"rub": 449_00, "stars": 250, "tag": " −32%"},
}
# Старые пакеты — нужны только для подсчёта выручки по старым оплатам.
LEGACY_RUB = {14: 70_00, 20: 90_00, 40: 150_00}
LEGACY_STARS = {14: 39, 20: 50, 40: 84}

COST_NUM = 3
WELCOME_BONUS = 3          # хватает на любой платный разбор
REF_FRIEND_BONUS = 2       # приглашённому сверху приветственных
BONUS_REF = 3              # пригласившему — после того как друг прошёл подписку
MSK = dt.timezone(dt.timedelta(hours=3))
TG_LIMIT = 4000

TAROT_TOPICS = {
    "love": "Любовь и отношения",
    "work": "Работа и деньги",
    "self": "Я и мой путь",
    "general": "Общий расклад",
}
NUM_TOPICS = {
    "rel": "Отношения",
    "work": "Работа",
    "self": "Саморазвитие",
    "family": "Семья",
    "general": "Общий анализ",
}
COMPAT_INTRO = {
    "Карма": "Союз-урок: вы встретились, чтобы чему-то научить друг друга.",
    "Судьба": "Союз-опора: вас тянет друг к другу не случайно.",
    "Зеркало": "Союз-зеркало: партнёр показывает тебе твои же качества.",
}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# ── мелкие утилиты ───────────────────────────────────
def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    n %= 10
    if n == 1:
        return one
    if 1 < n < 5:
        return few
    return many


def plural_ball(n: int) -> str:
    return plural(n, "балл", "балла", "баллов")


def balls(n: int) -> str:
    return f"{n} {plural_ball(n)}"


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def today_msk() -> dt.date:
    return dt.datetime.now(MSK).date()


def ru_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


DATE_RE = re.compile(r"(\d{1,2})[.\-/ ](\d{1,2})[.\-/ ](\d{4})")


def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    d, mnt, y = (int(x) for x in m.groups())
    try:
        date = dt.date(y, mnt, d)
    except ValueError:
        return None
    if not (1900 <= y <= dt.date.today().year) or date > dt.date.today():
        return None
    return f"{d:02d}.{mnt:02d}.{y}"


def _digital_root(dob: str) -> int:
    n = sum(int(c) for c in re.findall(r"\d", dob))
    while n > 9:
        n = sum(int(c) for c in str(n))
    return n


def compatibility_label(dob1: str, dob2: str) -> str:
    s = (_digital_root(dob1) + _digital_root(dob2)) % 3
    return ("Зеркало", "Карма", "Судьба")[s]


def trim_to(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    return cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…"


def split_chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # очень длинный абзац режем жёстко
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:limit])
            para = para[limit:]
        cand = f"{cur}\n\n{para}" if cur else para
        if len(cand) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts


def ref_link(bot_username: str, uid: int) -> str:
    names = aliases_for(uid)
    return f"https://t.me/{bot_username}?start={names[0] if names else uid}"


def share_url(link: str, text: str) -> str:
    return f"https://t.me/share/url?url={quote(link)}&text={quote(text)}"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def credits_of(uid: int) -> int:
    return get_credits(uid, ADMIN_IDS)


# ── ЭКРАН: одно активное сообщение ──────────────────
# kind: menu   — текстовый экран, можно править и удалять;
#       photo  — экран-картинка (выбор карт), можно править и удалять;
#       invoice— счёт, удаляем при уходе;
#       report — готовый разбор: остаётся в чате, у него только снимаем кнопки.
DISPOSABLE = {"menu", "photo", "invoice", None}


class Screen:
    def __init__(self, bot, uid: int, chat_id: int, new: bool = False):
        self.bot, self.uid, self.chat_id, self.new = bot, uid, chat_id, new

    async def _strip(self, msg_id: int):
        with contextlib.suppress(BadRequest, Forbidden):
            await self.bot.edit_message_reply_markup(self.chat_id, msg_id, reply_markup=None)

    async def _drop(self, msg_id: int, kind: str | None):
        if kind in DISPOSABLE:
            try:
                await self.bot.delete_message(self.chat_id, msg_id)
                return
            except (BadRequest, Forbidden):
                pass
        await self._strip(msg_id)

    async def show(self, text: str, kb: Mk | None = None, *, photo: bytes | None = None,
                   kind: str = "menu") -> int:
        cur, cur_kind = get_screen(self.uid)
        new, self.new = self.new, False
        if cur and not new:
            try:
                if photo is None and cur_kind in ("menu", None):
                    await self.bot.edit_message_text(
                        text, chat_id=self.chat_id, message_id=cur, reply_markup=kb,
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    )
                    set_screen(self.uid, cur, kind)
                    return cur
                if photo is not None and cur_kind == "photo":
                    await self.bot.edit_message_media(
                        InputMediaPhoto(photo, caption=text, parse_mode=ParseMode.HTML),
                        chat_id=self.chat_id, message_id=cur, reply_markup=kb,
                    )
                    set_screen(self.uid, cur, kind)
                    return cur
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    set_screen(self.uid, cur, kind)
                    return cur
                log.info("screen edit failed (%s), sending new", e)
        if photo is not None:
            m = await self.bot.send_photo(self.chat_id, photo, caption=text,
                                          parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            m = await self.bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML,
                                            reply_markup=kb, disable_web_page_preview=True)
        if cur and cur != m.message_id:
            await self._drop(cur, cur_kind)
        set_screen(self.uid, m.message_id, kind)
        return m.message_id

    async def caption(self, text: str, kb: Mk | None = None, *, kind: str = "photo"):
        """Меняет подпись текущего фото-экрана (без перезагрузки картинки)."""
        cur, cur_kind = get_screen(self.uid)
        if cur and cur_kind == "photo":
            try:
                await self.bot.edit_message_caption(chat_id=self.chat_id, message_id=cur, caption=text,
                                                    parse_mode=ParseMode.HTML, reply_markup=kb)
                set_screen(self.uid, cur, kind)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    set_screen(self.uid, cur, kind)
                    return
        await self.show(text, kb, kind="menu" if kind == "photo" else kind)

    async def show_long(self, text: str, kb: Mk | None, kind: str = "report"):
        """Длинный разбор: хвост — экран с кнопками, начало — обычные сообщения."""
        chunks = split_chunks(text)
        if len(chunks) > 1:
            self.new = True
            cur, cur_kind = get_screen(self.uid)
            if cur:
                await self._drop(cur, cur_kind)
                set_screen(self.uid, None, None)
            for chunk in chunks[:-1]:
                await self.bot.send_message(self.chat_id, chunk, parse_mode=ParseMode.HTML,
                                            disable_web_page_preview=True)
        await self.show(chunks[-1], kb, kind=kind)

    async def invoice(self, **kwargs):
        cur, cur_kind = get_screen(self.uid)
        m = await self.bot.send_invoice(chat_id=self.chat_id, **kwargs)
        if cur and cur != m.message_id:
            await self._drop(cur, cur_kind)
        set_screen(self.uid, m.message_id, "invoice")


def screen_for(update: Update, ctx, new: bool = False) -> Screen:
    return Screen(ctx.bot, update.effective_user.id, update.effective_chat.id, new)


# ── блокировки и «занят» ─────────────────────────────
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class TypingCtx:
    def __init__(self, bot, chat_id, interval=4.5):
        self.bot, self.chat_id, self.interval, self.task = bot, chat_id, interval, None

    async def _run(self):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            while True:
                await self.bot.send_chat_action(self.chat_id, "typing")
                await asyncio.sleep(self.interval)

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run())

    async def __aexit__(self, *exc):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


async def run_blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ── клавиатуры ───────────────────────────────────────
BACK = Btn("← Назад", callback_data="m")
HOME = Btn("🏠 Меню", callback_data="m")


def kb_main(uid: int) -> Mk:
    opened = get_daily(uid, today_msk().isoformat())
    daily = "🌙 Моя карта дня" if opened else "🌙 Карта дня · бесплатно"
    return Mk([
        [Btn(daily, callback_data="d")],
        [Btn("🃏 Расклад Таро", callback_data="t"), Btn("🔢 Нумерология", callback_data="n")],
        [Btn("💎 Пополнить", callback_data="pay"), Btn("🎁 Баллы за друга", callback_data="ref")],
        [Btn("❓ Как это работает", callback_data="how"), Btn("🛟 Поддержка", url=SUPPORT_URL)],
    ])


def kb_pay(back: str = "m") -> Mk:
    rows = []
    for qty, p in PACKS.items():
        row = []
        if YK_TOKEN:
            row.append(Btn(f"{qty} {plural_ball(qty)} · {p['rub'] // 100} ₽{p['tag']}",
                           callback_data=f"buy:{qty}:card"))
        row.append(Btn(f"{qty} · {p['stars']} ⭐", callback_data=f"buy:{qty}:star"))
        rows.append(row)
    rows.append([Btn("🎁 Бесплатно: пригласи друга", callback_data="ref")])
    rows.append([Btn("← Назад", callback_data=back)])
    return Mk(rows)


def kb_spreads() -> Mk:
    s = deck.SPREADS
    return Mk([
        [Btn(f"🎴 Совет карты · {balls(s[1]['cost'])}", callback_data="ts:1")],
        [Btn(f"🃏 Три карты · {balls(s[3]['cost'])}", callback_data="ts:3")],
        [Btn(f"🔮 Большой расклад · {balls(s[5]['cost'])}", callback_data="ts:5")],
        [BACK],
    ])


def kb_tarot_topics() -> Mk:
    return Mk([
        [Btn("❤️ Любовь и отношения", callback_data="tt:love")],
        [Btn("💼 Работа и деньги", callback_data="tt:work")],
        [Btn("🌱 Я и мой путь", callback_data="tt:self")],
        [Btn("🔮 Общий расклад", callback_data="tt:general")],
        [Btn("← Назад", callback_data="t")],
    ])


def kb_pick(n_options: int, step: int) -> Mk:
    btns = [Btn("🎴", callback_data=f"tp:{i}:{step}") for i in range(n_options)]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([Btn("✖ Отменить", callback_data="m")])
    return Mk(rows)


def kb_num_topics(has_dob: bool) -> Mk:
    rows = [
        [Btn("💞 Отношения и совместимость", callback_data="nt:rel")],
        [Btn("💼 Работа и деньги", callback_data="nt:work")],
        [Btn("🌱 Саморазвитие", callback_data="nt:self"), Btn("👨‍👩‍👧 Семья", callback_data="nt:family")],
        [Btn("✨ Общий разбор", callback_data="nt:general")],
    ]
    if has_dob:
        rows.append([Btn("✏️ Изменить дату рождения", callback_data="nd")])
    rows.append([BACK])
    return Mk(rows)


def kb_gender() -> Mk:
    return Mk([
        [Btn("Мужчина", callback_data="ng:m"), Btn("Женщина", callback_data="ng:f")],
        [BACK],
    ])


def kb_sub() -> Mk:
    return Mk([
        [Btn("📢 Подписаться на канал", url=JOIN_URL)],
        [Btn("✅ Я подписался", callback_data="chk_sub")],
    ])


def kb_after(ctx, uid: int, upsell: Btn | None, share_text: str) -> Mk:
    link = ref_link(ctx.bot.username, uid)
    rows = []
    if upsell:
        rows.append([upsell])
    rows.append([Btn("📤 Поделиться", url=share_url(link, share_text)), HOME])
    return Mk(rows)


# ── тексты экранов ───────────────────────────────────
HOW_IT_WORKS = (
    "❓ <b>Как это работает</b>\n\n"
    "🌙 <b>Карта дня</b> — каждый день бесплатно. Одна карта и короткое послание: "
    "на что опереться сегодня и чего избегать.\n\n"
    "🃏 <b>Таро</b> — выбираешь формат и тему (или пишешь свой вопрос), сам вытягиваешь "
    "карты из перемешанной колоды. Получаешь толкование каждой карты в её позиции и "
    "конкретные шаги — что делать дальше.\n\n"
    "🔢 <b>Нумерология</b> — по дате рождения считаем число судьбы, число дня рождения "
    "и твой личный год, а затем разбираем выбранную сферу. Для отношений — совместимость по двум датам.\n\n"
    "💎 <b>Баллы</b>: совет карты — 1, три карты — 2, большой расклад или нумерология — 3. "
    "Баллы не сгорают. За каждого друга — +3.\n\n"
    "<i>Расклады — инструмент для размышления, а не приговор. Решения всегда за тобой.</i>"
)


def welcome_banner(bonus: int) -> str:
    return (
        "✨ <b>Добро пожаловать!</b>\n"
        "Я раскладываю Таро и делаю нумерологический разбор — спокойно, по делу и без страшилок.\n\n"
        f"🎁 На баланс уже начислено <b>{balls(bonus)}</b> — хватит на любой расклад.\n"
        "🌙 А карта дня — бесплатно каждый день."
    )


def main_text(uid: int, banner: str | None = None) -> str:
    credits = credits_of(uid)
    daily = get_daily(uid, today_msk().isoformat())
    lines = []
    if banner:
        lines += [banner, "", "———"]
    lines.append("🔮 <b>Таро и нумерология</b>\n")
    lines.append(f"💎 Баланс: <b>{'∞' if is_admin(uid) else balls(credits)}</b>")
    if daily:
        lines.append(f"🌙 Карта дня: <b>{esc(deck.card_label(daily[0], daily[1]))}</b>")
    else:
        lines.append("🌙 Карта дня ещё не открыта — это бесплатно")
    lines.append("\nЧто сделаем?")
    return "\n".join(lines)


async def show_main(scr: Screen, banner: str | None = None):
    await scr.show(main_text(scr.uid, banner), kb_main(scr.uid))


def pay_text(uid: int, need: str | None = None) -> str:
    lines = []
    if need:
        lines += [f"⚠️ {need}", ""]
    lines += [
        f"💎 <b>Баланс: {balls(credits_of(uid))}</b>\n",
        "Сколько стоит:",
        "🎴 Совет карты — 1 балл",
        "🃏 Три карты — 2 балла",
        "🔮 Большой расклад или нумерология — 3 балла",
        "🌙 Карта дня — всегда бесплатно\n",
        "Баллы не сгорают. Выбери пакет:",
    ]
    return "\n".join(lines)


async def paywall(scr: Screen, what: str, cost: int):
    have = credits_of(scr.uid)
    log_event("paywall", scr.uid)
    await scr.show(pay_text(scr.uid, f"Для «{what}» нужно {balls(cost)}, у тебя {have}."), kb_pay())


# ── подписка на канал ────────────────────────────────
async def is_subscribed(bot, uid: int, force: bool = False) -> bool:
    if is_admin(uid) or not CHANNEL_ID:
        return True
    sub, last = get_subscribed(uid)
    now = int(time.time())
    if sub and not force and now - (last or 0) < SUB_CACHE_SEC:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        ok = m.status in {"member", "administrator", "creator"} or (
            m.status == "restricted" and getattr(m, "is_member", False)
        )
    except BadRequest as e:
        msg = str(e).lower()
        ok = not ("user not found" in msg or "participant" in msg)
        if ok:
            log.warning("subscription check misconfigured: %s", e)
    except (Forbidden, NetworkError, TimedOut) as e:
        log.warning("subscription check failed, letting user in: %s", e)
        return True
    set_subscribed(uid, ok, now)
    return ok


async def show_gate(scr: Screen, bonus: int | None = None):
    text = (
        "🔮 <b>Почти готово</b>\n\n"
        "Бот работает для подписчиков нашего канала — там карты недели, разборы и бонусы.\n"
        "Подпишись и нажми «Я подписался» — сразу начнём."
    )
    if bonus:
        text += f"\n\n🎁 Тебя уже ждут <b>{balls(bonus)}</b> на балансе."
    await scr.show(text, kb_sub())


async def reward_referrer(ctx, child_id: int, child_name: str):
    parent = claim_ref_reward(child_id)
    if not parent:
        return
    add_credits(parent, BONUS_REF)
    banner = f"🎉 По твоей ссылке пришёл {esc(child_name)}! Тебе +{balls(BONUS_REF)}."
    try:
        await show_main(Screen(ctx.bot, parent, parent, new=True), banner)
    except (BadRequest, Forbidden, NetworkError):
        pass


async def after_gate(update: Update, ctx, scr: Screen):
    await reward_referrer(ctx, scr.uid, update.effective_user.first_name or "друг")
    bonus = ctx.user_data.pop("welcome", None)
    await show_main(scr, welcome_banner(bonus) if bonus else None)


# ── /start ───────────────────────────────────────────
def resolve_ref(payload: str) -> int | None:
    if not payload:
        return None
    if payload.isdigit():
        uid = int(payload)
        return None if is_new_user(uid) else uid
    return alias_owner(payload.lower())


async def start(update: Update, ctx):
    uid = update.effective_user.id
    async with _locks[uid]:
        with contextlib.suppress(BadRequest, Forbidden):
            await update.message.delete()
        payload = ctx.args[0] if ctx.args else ""
        if register_user(uid):
            parent = resolve_ref(payload)
            referred = bool(parent and set_referral(uid, parent))
            bonus = WELCOME_BONUS + (REF_FRIEND_BONUS if referred else 0)
            add_credits(uid, bonus)
            ctx.user_data["welcome"] = bonus
            log_event("start_new", uid)
            if payload and not referred:
                log_event(f"src_{payload[:40]}", uid)
        log_event("start", uid)
        ctx.user_data.pop("flow", None)
        scr = screen_for(update, ctx, new=True)
        if not await is_subscribed(ctx.bot, uid, force=True):
            await show_gate(scr, ctx.user_data.get("welcome"))
            return
        await after_gate(update, ctx, scr)


# ── Таро ─────────────────────────────────────────────
OPTIONS_PER_STEP = 6


def tarot_caption(flow: dict) -> str:
    sp = deck.SPREADS[flow["spread"]]
    lines = [f"🃏 <b>{sp['title']}</b> · {esc(flow['topic'])}"]
    if flow.get("question"):
        lines.append(f"❔ <i>{esc(flow['question'])}</i>")
    return "\n".join(lines)


def tarot_slots(flow: dict) -> list:
    n = flow["spread"]
    picked = flow["picked"]
    return [tuple(picked[i]) if i < len(picked) else None for i in range(n)]


async def tarot_pick_screen(scr: Screen, flow: dict):
    n, step = flow["spread"], len(flow["picked"])
    pos = deck.SPREADS[n]["positions"][step]
    flow["options"] = flow["deck"][:OPTIONS_PER_STEP]
    img = await run_blocking(deck.render_spread, tarot_slots(flow), step)
    text = tarot_caption(flow)
    text += "\n\n" + ("Сосредоточься на вопросе и вытяни карту." if n == 1 else
                      f"Сосредоточься и вытяни карту <b>{step + 1} из {n}</b> — «{pos}».")
    await scr.show(text, kb_pick(len(flow["options"]), step), photo=img, kind="photo")


async def tarot_start(scr: Screen, ctx, topic: str, question: str | None = None):
    flow = ctx.user_data.get("flow") or {}
    if flow.get("kind") != "tarot" or not flow.get("spread"):
        await show_main(scr)
        return
    flow.update(topic=topic, question=question, deck=deck.shuffled_deck(), picked=[])
    flow.pop("await", None)
    await tarot_pick_screen(scr, flow)


async def tarot_finish(update: Update, ctx, scr: Screen, flow: dict):
    uid, n = scr.uid, flow["spread"]
    sp = deck.SPREADS[n]
    if not spend_credits(uid, sp["cost"], ADMIN_IDS):
        ctx.user_data.pop("flow", None)
        await paywall(scr, sp["title"], sp["cost"])
        return
    _, gender = get_profile(uid)
    cards = [
        {"position": pos, "card": deck.card_by_id(cid)["name"], "reversed": rev}
        for pos, (cid, rev) in zip(sp["positions"], flow["picked"])
    ]
    img = await run_blocking(deck.render_spread, tarot_slots(flow), None)
    await scr.show(tarot_caption(flow) + "\n\n✨ Карты легли. Толкую расклад…", None, photo=img, kind="photo")
    try:
        async with TypingCtx(ctx.bot, scr.chat_id):
            report = await run_blocking(build_tarot, cards, n, flow["topic"], flow.get("question"), gender)
    except Exception as e:
        log.error("tarot LLM failed: %s", e)
        refund_credits(uid, sp["cost"], ADMIN_IDS)
        await scr.caption(
            tarot_caption(flow) + "\n\n😔 Не получилось дозваться толкования. Баллы вернул — попробуй ещё раз.",
            Mk([[Btn("🔁 Повторить", callback_data="tretry")], [HOME]]),
        )
        flow["failed"] = True
        return
    ctx.user_data.pop("flow", None)
    log_event("tarotA", uid)
    log_event(f"spread_{n}", uid)
    log_ab_event(uid, "tarot", "A", len(report))
    summary = "\n".join(
        f"{i + 1}. {pos} — <b>{esc(deck.card_label(cid, rev))}</b>" if n > 1 else
        f"🎴 <b>{esc(deck.card_label(cid, rev))}</b>"
        for i, (pos, (cid, rev)) in enumerate(zip(sp["positions"], flow["picked"]))
    )
    await scr.caption(tarot_caption(flow) + "\n\n" + summary, None, kind="report")
    upsell = {
        1: Btn(f"🃏 Разложить три карты · {balls(2)}", callback_data="ts:3"),
        3: Btn(f"🔮 Большой расклад · {balls(3)}", callback_data="ts:5"),
        5: Btn(f"🔢 Нумерология · {balls(COST_NUM)}", callback_data="n"),
    }[n]
    first = deck.card_by_id(flow["picked"][0][0])["name"]
    body = f"🃏 <b>{sp['title']}</b> · {esc(flow['topic'])}\n\n{esc(report)}"
    await scr.show_long(body, kb_after(ctx, uid, upsell, f"Мне выпала карта «{first}» 🔮 Попробуй и ты:"))


# ── Карта дня ───────────────────────────────────────
async def daily_card(update: Update, ctx, scr: Screen):
    uid = scr.uid
    day = today_msk()
    cached = get_daily(uid, day.isoformat())
    if cached:
        cid, rev, text = cached
    else:
        cid, rev = deck.daily_pick(uid, day.isoformat())
        _, gender = get_profile(uid)
        await scr.show("🌙 Тяну твою карту дня…", None)
        try:
            async with TypingCtx(ctx.bot, scr.chat_id):
                text = await run_blocking(build_daily, deck.card_label(cid, rev), rev, gender)
        except Exception as e:
            log.error("daily LLM failed: %s", e)
            await show_main(scr, "😔 Карта дня пока не открылась. Попробуй ещё раз через минуту.")
            return
        save_daily(uid, day.isoformat(), cid, rev, text)
        log_event("daily", uid)
    label = deck.card_label(cid, rev)
    header = f"🌙 <b>Карта дня · {ru_date(day)}</b>\n<b>{esc(label)}</b>\n\n"
    caption = header + esc(trim_to(text, 1000 - len(header)))
    img = await run_blocking(deck.render_single, cid, rev)
    kb = kb_after(ctx, uid, Btn("🃏 Расклад на три карты", callback_data="ts:3"),
                  f"Моя карта дня — «{deck.card_by_id(cid)['name']}» 🌙 Открой свою бесплатно:")
    await scr.show(caption, kb, photo=img, kind="report")


# ── Нумерология ──────────────────────────────────────
NUM_INTRO = (
    f"🔢 <b>Нумерология</b> · {balls(COST_NUM)}\n\n"
    "По дате рождения посчитаю твоё число судьбы, число дня рождения и личный год, "
    "а потом разберу выбранную сферу: сильные стороны, слабые места и на что направить силы сейчас."
)


async def num_ask_dob(scr: Screen, ctx, which: str = "dob1", error: bool = False):
    flow = ctx.user_data.setdefault("flow", {"kind": "num"})
    flow["await"] = which
    if which == "dob1":
        text = NUM_INTRO + "\n\n✍️ Напиши свою дату рождения, например <b>07.03.1994</b>"
        kb = Mk([[BACK]])
    else:
        who = "партнёра" if flow.get("topic") == "Отношения" else "члена семьи"
        text = (f"💞 Для совместимости напиши дату рождения {who}, например <b>21.11.1992</b>.\n\n"
                "Или пропусти — разберу тему только по твоей дате.")
        kb = Mk([[Btn("Пропустить →", callback_data="nskip")], [Btn("← Назад", callback_data="n")]])
    if error:
        text = "⚠️ Не получилось прочитать дату. Формат: ДД.ММ.ГГГГ\n\n" + text
    await scr.show(text, kb)


async def num_entry(scr: Screen, ctx):
    uid = scr.uid
    if credits_of(uid) < COST_NUM:
        await paywall(scr, "Нумерология", COST_NUM)
        return
    ctx.user_data["flow"] = {"kind": "num"}
    dob, gender = get_profile(uid)
    if not dob:
        await num_ask_dob(scr, ctx)
    elif not gender:
        await scr.show(NUM_INTRO + "\n\nУточни пол — так толкование будет точнее:", kb_gender())
    else:
        await scr.show(NUM_INTRO + f"\n\n📅 Твоя дата: <b>{dob}</b>\nВыбери сферу:", kb_num_topics(True))


async def num_run(update: Update, ctx, scr: Screen):
    uid = scr.uid
    flow = ctx.user_data.get("flow") or {}
    dob, gender = get_profile(uid)
    topic = flow.get("topic") or "Общий анализ"
    dob2 = flow.get("dob2")
    if not dob:
        await num_ask_dob(scr, ctx)
        return
    if not spend_credits(uid, COST_NUM, ADMIN_IDS):
        ctx.user_data.pop("flow", None)
        await paywall(scr, "Нумерология", COST_NUM)
        return
    nums = numerology.profile(dob, today_msk())
    pair = numerology.pair(dob, dob2) if dob2 else None
    await scr.show("🔢 Считаю числа и готовлю разбор — это займёт до минуты…", None)
    try:
        async with TypingCtx(ctx.bot, scr.chat_id):
            report = await run_blocking(build_numerology, dob, nums, topic, gender, dob2, pair)
    except Exception as e:
        log.error("numerology LLM failed: %s", e)
        refund_credits(uid, COST_NUM, ADMIN_IDS)
        await scr.show("😔 Не получилось подготовить разбор. Баллы вернул — попробуй ещё раз.",
                       Mk([[Btn("🔁 Повторить", callback_data="nretry")], [HOME]]))
        return
    ctx.user_data.pop("flow", None)
    log_event("numA", uid)
    log_ab_event(uid, "num", "A", len(report))
    head = [f"🔢 <b>Нумерология · {esc(topic)}</b>", esc(numerology.header_line(nums))]
    if pair and topic == "Отношения":
        label = compatibility_label(dob, dob2)
        head.append(f"💞 Числа пары: {pair['life_path_1']} и {pair['life_path_2']} · {label}. "
                    f"{COMPAT_INTRO[label]}")
    elif pair:
        head.append(f"👨‍👩‍👧 Числа судьбы: твоё {pair['life_path_1']}, родственника {pair['life_path_2']}")
    upsell = (Btn("🃏 Расклад Таро на эту тему", callback_data="t") if pair else
              Btn(f"💞 Совместимость · {balls(COST_NUM)}", callback_data="nt:rel"))
    body = "\n".join(head) + "\n\n" + esc(report)
    await scr.show_long(body, kb_after(ctx, uid, upsell,
                                       f"Моё число судьбы — {nums['life_path']} 🔢 Узнай своё:"))


# ── Роутер кнопок ────────────────────────────────────
LEGACY = {"back": "m", "home": "m", "num": "n", "tarot": "t", "back_pay": "pay"}


async def router(update: Update, ctx):
    q = update.callback_query
    uid = update.effective_user.id
    data = LEGACY.get(q.data, q.data or "")
    if ctx.user_data.get("busy"):
        await q.answer("⏳ Ещё готовлю прошлый ответ…")
        return
    async with _locks[uid]:
        cur, _ = get_screen(uid)
        fresh = q.message is None or q.message.message_id != cur
        if fresh and q.message is not None:
            with contextlib.suppress(BadRequest, Forbidden):
                await q.message.edit_reply_markup(reply_markup=None)
        scr = screen_for(update, ctx, new=fresh)

        if data == "chk_sub":
            if await is_subscribed(ctx.bot, uid, force=True):
                await q.answer("Подписка подтверждена ✨")
                await after_gate(update, ctx, scr)
            else:
                await q.answer("Подписка пока не видна. Подпишись и нажми ещё раз.", show_alert=True)
            return
        if not await is_subscribed(ctx.bot, uid):
            await q.answer()
            await show_gate(scr)
            return
        with contextlib.suppress(BadRequest):
            await q.answer()
        ctx.user_data["busy"] = True
        try:
            await dispatch(update, ctx, scr, data)
        finally:
            ctx.user_data["busy"] = False


async def dispatch(update: Update, ctx, scr: Screen, data: str):
    uid = scr.uid
    flow = ctx.user_data.get("flow") or {}

    if data == "m":
        ctx.user_data.pop("flow", None)
        await show_main(scr)
    elif data == "d":
        await daily_card(update, ctx, scr)
    elif data == "how":
        await scr.show(HOW_IT_WORKS, Mk([[Btn("🌙 Открыть карту дня", callback_data="d")], [BACK]]))

    # Таро
    elif data == "t":
        if credits_of(uid) < 1:
            await paywall(scr, "Расклад Таро", 1)
            return
        ctx.user_data["flow"] = {"kind": "tarot"}
        await scr.show(
            "🃏 <b>Расклад Таро</b>\n\n"
            "🎴 <b>Совет карты</b> — один ясный ответ на вопрос\n"
            "🃏 <b>Три карты</b> — прошлое, настоящее и куда всё идёт\n"
            "🔮 <b>Большой расклад</b> — ситуация, препятствие, совет, ресурс и итог\n\n"
            "Выбери формат:",
            kb_spreads(),
        )
    elif data.startswith("ts:"):
        n = int(data[3:]) if data[3:] in ("1", "3", "5") else 3
        sp = deck.SPREADS[n]
        if credits_of(uid) < sp["cost"]:
            await paywall(scr, sp["title"], sp["cost"])
            return
        ctx.user_data["flow"] = {"kind": "tarot", "spread": n, "await": "question"}
        log_event(f"spread_pick_{n}", uid)
        await scr.show(
            f"🃏 <b>{sp['title']}</b> · {balls(sp['cost'])}\n\n"
            "О чём спросим карты? Выбери тему —\n"
            "или ✍️ <b>напиши свой вопрос сообщением</b>, так ответ будет точнее.",
            kb_tarot_topics(),
        )
    elif data.startswith("tt:"):
        key = data[3:]
        log_event(f"ttopic_{key}", uid)
        await tarot_start(scr, ctx, TAROT_TOPICS.get(key, "Общий расклад"))
    elif data.startswith("tp:"):
        try:
            _, idx, step = data.split(":")
            idx, step = int(idx), int(step)
        except ValueError:
            return
        if flow.get("kind") != "tarot" or "picked" not in flow or step != len(flow["picked"]) \
                or idx >= len(flow.get("options", [])):
            if flow.get("kind") != "tarot" or "picked" not in flow:
                await show_main(scr, "Этот расклад уже закрыт — начни новый 🙂")
            return
        cid = flow["options"][idx]
        flow["deck"].remove(cid)
        flow["picked"].append((cid, deck.draw_reversed()))
        if len(flow["picked"]) < flow["spread"]:
            await tarot_pick_screen(scr, flow)
        else:
            await tarot_finish(update, ctx, scr, flow)
    elif data == "tretry":
        if flow.get("kind") == "tarot" and flow.get("failed"):
            flow.pop("failed", None)
            await tarot_finish(update, ctx, scr, flow)
        else:
            await show_main(scr)

    # Нумерология
    elif data == "n":
        await num_entry(scr, ctx)
    elif data == "nd":
        ctx.user_data["flow"] = {"kind": "num"}
        await num_ask_dob(scr, ctx)
    elif data.startswith("ng:"):
        set_profile(uid, gender="мужчина" if data.endswith("m") else "женщина")
        dob, _ = get_profile(uid)
        ctx.user_data["flow"] = {"kind": "num"}
        await scr.show(NUM_INTRO + f"\n\n📅 Твоя дата: <b>{dob}</b>\nВыбери сферу:", kb_num_topics(True))
    elif data.startswith("nt:"):
        key = data[3:]
        topic = NUM_TOPICS.get(key, "Общий анализ")
        log_event(f"topic_{key}", uid)
        if credits_of(uid) < COST_NUM:
            await paywall(scr, "Нумерология", COST_NUM)
            return
        dob, _ = get_profile(uid)
        ctx.user_data["flow"] = {"kind": "num", "topic": topic}
        if not dob:
            await num_ask_dob(scr, ctx)
        elif topic in ("Отношения", "Семья"):
            await num_ask_dob(scr, ctx, "dob2")
        else:
            await num_run(update, ctx, scr)
    elif data in ("nskip", "nretry"):
        if flow.get("kind") == "num":
            await num_run(update, ctx, scr)
        else:
            await show_main(scr)

    # Оплата и рефералка
    elif data == "pay":
        log_event("pay", uid)
        await scr.show(pay_text(uid), kb_pay())
    elif data.startswith("buy:"):
        _, qty, method = data.split(":")
        qty = int(qty)
        pack = PACKS.get(qty)
        if not pack or (method == "card" and not YK_TOKEN):
            await scr.show(pay_text(uid), kb_pay())
            return
        log_event(f"buy_{qty}_{method}", uid)
        star = method != "card"
        await scr.invoice(
            title=f"{qty} {plural_ball(qty)}",
            description=(f"Хватит на {qty // 2} раскладов на три карты или {qty // 3} больших разборов. "
                         "Баллы не сгорают."),
            payload=f"buy_{qty}_{method}",
            provider_token="" if star else YK_TOKEN,
            currency="XTR" if star else "RUB",
            prices=[LabeledPrice(f"{qty} {plural_ball(qty)}", pack["stars"] if star else pack["rub"])],
            reply_markup=Mk([[Btn("Оплатить", pay=True)], [Btn("← Назад", callback_data="pay")]]),
        )
    elif data == "ref":
        await show_ref(scr, ctx)
    else:
        # Кнопки из старой версии бота (spread_*, choose:*, topic_* …)
        ctx.user_data.pop("flow", None)
        await show_main(scr, "Меню обновилось ✨ Начнём заново:")


async def show_ref(scr: Screen, ctx):
    uid = scr.uid
    link = ref_link(ctx.bot.username, uid)
    text = (
        "🎁 <b>Баллы за друзей</b>\n\n"
        f"Отправь другу ссылку. Когда он запустит бота, тебе — <b>+{balls(BONUS_REF)}</b>, "
        f"а ему — <b>+{balls(REF_FRIEND_BONUS)}</b> к приветственным.\n\n"
        f"👥 Уже пришло: <b>{referral_count(uid)}</b>\n"
        f"🔗 <code>{esc(link)}</code>"
    )
    kb = Mk([
        [Btn("📤 Отправить другу", url=share_url(link, "Тут бесплатная карта дня и расклады Таро 🔮"))],
        [Btn("📋 Скопировать ссылку", copy_text=CopyTextButton(link))],
        [BACK],
    ])
    await scr.show(text, kb)


# ── Текстовые сообщения ──────────────────────────────
async def handle_text(update: Update, ctx):
    uid = update.effective_user.id
    msg = update.message
    with contextlib.suppress(BadRequest, Forbidden):
        await msg.delete()  # держим чат чистым: ввод «впитывается» экраном
    if ctx.user_data.get("busy"):
        return
    async with _locks[uid]:
        scr = screen_for(update, ctx)
        if not await is_subscribed(ctx.bot, uid):
            await show_gate(scr)
            return
        flow = ctx.user_data.get("flow") or {}
        wait = flow.get("await")
        ctx.user_data["busy"] = True
        try:
            if flow.get("kind") == "num" and wait == "dob1":
                dob = parse_date(msg.text)
                if not dob:
                    await num_ask_dob(scr, ctx, "dob1", error=True)
                    return
                set_profile(uid, dob=dob)
                flow.pop("await", None)
                _, gender = get_profile(uid)
                if not gender:
                    await scr.show(f"📅 Дата: <b>{dob}</b>\n\nУточни пол — так толкование будет точнее:",
                                   kb_gender())
                elif flow.get("topic"):
                    if flow["topic"] in ("Отношения", "Семья"):
                        await num_ask_dob(scr, ctx, "dob2")
                    else:
                        await num_run(update, ctx, scr)
                else:
                    await scr.show(NUM_INTRO + f"\n\n📅 Твоя дата: <b>{dob}</b>\nВыбери сферу:",
                                   kb_num_topics(True))
            elif flow.get("kind") == "num" and wait == "dob2":
                dob2 = parse_date(msg.text)
                if not dob2:
                    await num_ask_dob(scr, ctx, "dob2", error=True)
                    return
                flow["dob2"] = dob2
                flow.pop("await", None)
                await num_run(update, ctx, scr)
            elif flow.get("kind") == "tarot" and wait == "question":
                question = re.sub(r"\s+", " ", msg.text or "").strip()[:300]
                log_event("tquestion", uid)
                await tarot_start(scr, ctx, "Свой вопрос", question)
            else:
                await show_main(scr)
        finally:
            ctx.user_data["busy"] = False


# ── Команды-ярлыки ───────────────────────────────────
def shortcut(action: str):
    async def handler(update: Update, ctx):
        uid = update.effective_user.id
        with contextlib.suppress(BadRequest, Forbidden):
            await update.message.delete()
        if ctx.user_data.get("busy"):
            return
        async with _locks[uid]:
            scr = screen_for(update, ctx, new=True)
            if not await is_subscribed(ctx.bot, uid):
                await show_gate(scr)
                return
            ctx.user_data["busy"] = True
            try:
                await dispatch(update, ctx, scr, action)
            finally:
                ctx.user_data["busy"] = False
    return handler


# ── Оплата ───────────────────────────────────────────
PAYLOAD_RE = re.compile(r"^buy_(\d+)_(card|star)$")


async def pre_checkout(update: Update, ctx):
    q = update.pre_checkout_query
    if PAYLOAD_RE.match(q.invoice_payload or ""):
        await q.answer(True)
    else:
        await q.answer(False, "Счёт устарел — открой «Пополнить» ещё раз.")


async def paid(update: Update, ctx):
    sp = update.message.successful_payment
    uid = update.effective_user.id
    m = PAYLOAD_RE.match(sp.invoice_payload or "")
    qty = int(m.group(1)) if m else 0
    method = m.group(2) if m else "card"
    # Для Stars provider_payment_charge_id пустой — уникален только telegram_payment_charge_id.
    charge_id = sp.provider_payment_charge_id or sp.telegram_payment_charge_id
    log_event("paid", uid)
    if qty and log_payment(uid, qty, method, charge_id, sp.total_amount, sp.currency):
        add_credits(uid, qty)
    async with _locks[uid]:
        with contextlib.suppress(BadRequest, Forbidden):
            await update.message.delete()
        await show_main(screen_for(update, ctx, new=True),
                        f"✅ <b>Оплата прошла!</b> +{balls(qty)} на балансе. Спасибо 💜")


async def my_chat_member(update: Update, ctx):
    st = update.my_chat_member.new_chat_member.status
    if st in {"kicked", "left"}:
        log_event("bot_removed", update.effective_user.id)


# ── Пользовательские команды ─────────────────────────
async def profile(update: Update, ctx):
    await shortcut("ref")(update, ctx)


async def set_alias_cmd(update: Update, ctx):
    uid = update.effective_user.id
    alias = (ctx.args[0] if ctx.args else "").lower()
    if not re.fullmatch(r"[a-z0-9_]{3,32}", alias) or alias.startswith("site"):
        await update.message.reply_text("Использование: /alias имя (латиница, цифры, _; 3–32 символа)")
        return
    if add_alias(uid, alias):
        await shortcut("ref")(update, ctx)
    else:
        await update.message.reply_text("❌ Это имя уже занято.")


def _pay_value(method: str, qty: int, amount) -> tuple[int, int]:
    """(рубли, звёзды) за одну оплату."""
    if method == "card":
        kop = amount if amount is not None else LEGACY_RUB.get(qty, 0)
        return kop // 100, 0
    return 0, amount if amount is not None else LEGACY_STARS.get(qty, 0)


async def my_referrals(update: Update, ctx):
    uid = update.effective_user.id
    rub = star = 0
    for _, method, qty, amount in referral_payment_rows(uid):
        r, s = _pay_value(method, qty, amount)
        rub, star = rub + r, star + s
    await update.message.reply_text(f"Пришло: {referral_count(uid)}\nОплатили: {rub} ₽, {star} ⭐")


# ── Админ ────────────────────────────────────────────
def admin_only(func):
    async def wrapper(update, ctx, *args, **kwargs):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper


async def ping(update: Update, ctx):
    await update.message.reply_text("pong")


@admin_only
async def health(update: Update, ctx):
    uptime = dt.datetime.now() - START_TIME
    try:
        ver = subprocess.check_output(["git", "describe", "--always", "--dirty"]).decode().strip()
    except Exception:
        ver = "unknown"
    await update.message.reply_text(
        f"uptime: {uptime}\ntasks: {len(asyncio.all_tasks())}\nllm queue: {llm_queue_size()}\nversion: {ver}"
    )


@admin_only
async def grant(update: Update, ctx):
    if len(ctx.args) not in (1, 2):
        await update.message.reply_text("Usage: /grant <qty> | /grant <id> <qty>")
        return
    if len(ctx.args) == 1:
        uid, qty = update.effective_user.id, int(ctx.args[0])
    else:
        uid, qty = int(ctx.args[0]), int(ctx.args[1])
    add_credits(uid, qty)
    await update.message.reply_text(f"✅ Теперь у {uid}: {credits_of(uid)}.")
    if uid != update.effective_user.id:
        try:
            await show_main(Screen(ctx.bot, uid, uid, new=True), f"🎁 Тебе подарок: +{balls(qty)}!")
        except (BadRequest, Forbidden):
            pass


@admin_only
async def broadcast(update: Update, ctx):
    """Рассылка приходит как баннер над главным меню — экран остаётся один."""
    text = update.message.text.split(maxsplit=1)[1] if len(update.message.text.split(maxsplit=1)) > 1 else ""
    if not text:
        await update.message.reply_text("Usage: /broadcast <text> (HTML разрешён)")
        return
    ok = fail = 0
    for uid in all_user_ids():
        try:
            await show_main(Screen(ctx.bot, uid, uid, new=True), text)
            ok += 1
        except (BadRequest, Forbidden):
            fail += 1
        except Exception as e:
            fail += 1
            log.warning("Broadcast to %s failed: %s", uid, e)
    await update.message.reply_text(f"✅ Рассылка завершена: {ok} доставлено, {fail} нет.")


def _revenue(rows) -> tuple[int, int, dict, dict]:
    rub = star = 0
    card_packs: dict[int, int] = {}
    star_packs: dict[int, int] = {}
    for method, qty, amount, _cur, _uid in rows:
        r, s = _pay_value(method, qty, amount)
        rub, star = rub + r, star + s
        packs = card_packs if method == "card" else star_packs
        packs[qty] = packs.get(qty, 0) + 1
    return rub, star, card_packs, star_packs


@admin_only
async def stats(update: Update, ctx):
    events = event_stats()
    rub, star, card_packs, star_packs = _revenue(payment_rows())
    rub30, star30, _, _ = _revenue(payment_rows(30))
    d_cnt, d_users = daily_stats(1)
    num_cnt = events.get("numA", 0) + events.get("numB", 0)
    tarot_cnt = events.get("tarotA", 0) + events.get("tarotB", 0)
    rows = [
        ("Новых за 1д", count_new_users(1)),
        ("за 7д", count_new_users(7)),
        ("за 30д", count_new_users(30)),
        ("Карт дня за 24ч", f"{d_users}/{d_cnt}"),
        ("Упёрлись в баланс", unique_event_users("paywall")),
        ("Открыли оплату", unique_event_users("pay")),
        ("Нажали пакет", unique_event_users_like("buy_")),
        ("Оплатили (людей)", unique_payers()),
        ("Сумма ₽", rub),
        ("Сумма ⭐", star),
        ("₽ за 30д", rub30),
        ("⭐ за 30д", star30),
        ("Нумерология", f"{unique_event_users_like('num')}/{num_cnt}"),
        ("Таро", f"{unique_event_users_like('tarot')}/{tarot_cnt}"),
        ("Свой вопрос", events.get("tquestion", 0)),
        ("Приглашено", sum(c for _, c in referral_counts())),
    ]
    for key, name in NUM_TOPICS.items():
        rows.append((f"Нумерол. {name}", events.get(f"topic_{key}", 0)))
    if card_packs:
        rows.append(("Пакеты ₽", ", ".join(f"{q}×{c}" for q, c in sorted(card_packs.items()))))
    if star_packs:
        rows.append(("Пакеты ⭐", ", ".join(f"{q}×{c}" for q, c in sorted(star_packs.items()))))
    src = sorted(((k[4:], v) for k, v in events.items() if k.startswith("src_")), key=lambda x: -x[1])[:8]
    for k, v in src:
        rows.append((f"Источник {k}", v))
    table = "\n".join(f"{k}: {v}" for k, v in rows)
    await update.message.reply_text(f"<b>Статистика</b>\n<pre>{esc(table)}</pre>", parse_mode=ParseMode.HTML)


@admin_only
async def stats_referrals(update: Update, ctx):
    refs = referral_counts()
    if not refs:
        await update.message.reply_text("Рефералов нет.")
        return
    money: dict[int, list[int]] = {}
    for parent, method, qty, amount in referral_payment_rows():
        r, s = _pay_value(method, qty, amount)
        acc = money.setdefault(parent, [0, 0])
        acc[0] += r
        acc[1] += s
    lines = []
    for uid, cnt in sorted(refs, key=lambda x: -x[1])[:50]:
        names = ", ".join(aliases_for(uid)) or "-"
        r, s = money.get(uid, [0, 0])
        lines.append(f"{uid} | {names} | {cnt} | {r} ₽ | {s} ⭐")
    await update.message.reply_text("<b>Рефералы</b>\n<pre>" + esc("\n".join(lines)) + "</pre>",
                                    parse_mode=ParseMode.HTML)


@admin_only
async def ab_stats(update: Update, ctx):
    rows = db.DB.execute(
        "SELECT variant, COUNT(*), AVG(length), COUNT(DISTINCT uid) FROM ab_events GROUP BY variant"
    ).fetchall()
    text = "\n".join(f"{v}: {c} ответов, {u} людей, ср. длина {int(a or 0)}" for v, c, a, u in rows)
    await update.message.reply_text(text or "Нет данных")


@admin_only
async def cards_check(update: Update, ctx):
    missing = [c["name"] for c in deck.all_cards() if not os.path.exists(c["path"])]
    await update.message.reply_text(
        f"Карт в колоде: {len(deck.all_cards())}" + (f"\nНет файлов: {', '.join(missing)}" if missing else " ✅")
    )


# ── Ошибки ───────────────────────────────────────────
async def err(update: object, ctx):
    e = ctx.error
    if isinstance(e, (NetworkError, TimedOut, RetryAfter)):
        log.warning("Network error: %s", e)
        return
    log.error("Exception:", exc_info=e)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("card", "Карта дня — бесплатно"),
        BotCommand("tarot", "Расклад Таро"),
        BotCommand("numerology", "Нумерология по дате рождения"),
        BotCommand("balance", "Баланс и пополнение"),
        BotCommand("friends", "Баллы за друзей"),
    ])


# ── main ─────────────────────────────────────────────
def main():
    req = HTTPXRequest(connect_timeout=10, read_timeout=30, write_timeout=30,
                       connection_pool_size=256, pool_timeout=10)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(64)
        .job_queue(JobQueue())
        .post_init(post_init)
        .build()
    )
    app.add_handlers([
        CommandHandler("start", start),
        CommandHandler("card", shortcut("d")),
        CommandHandler("tarot", shortcut("t")),
        CommandHandler("numerology", shortcut("n")),
        CommandHandler("balance", shortcut("pay")),
        CommandHandler("friends", shortcut("ref")),
        CommandHandler("profile", profile),
        CommandHandler("alias", set_alias_cmd),
        CommandHandler("refs", my_referrals),
        CommandHandler("grant", grant),
        CommandHandler("broadcast", broadcast),
        CommandHandler("stats", stats),
        CommandHandler("stats_referrals", stats_referrals),
        CommandHandler("ab_stats", ab_stats),
        CommandHandler("ping", ping),
        CommandHandler("health", health),
        CommandHandler("cards_check", cards_check),
        CallbackQueryHandler(router),
        PreCheckoutQueryHandler(pre_checkout),
        MessageHandler(filters.SUCCESSFUL_PAYMENT, paid),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
        ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
    ])
    app.add_error_handler(err)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
