# bot.py – версия 02-06-2025: фиксы оплаты, 1-й бесплатный анализ, уведомление grant
# -*- coding: utf-8 -*-
import os, logging, asyncio, contextlib, re, html, json, datetime as dt, subprocess, time
from pathlib import Path
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Mk,
    LabeledPrice,
    Message,
    CopyTextButton,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut, RetryAfter, Forbidden
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    PreCheckoutQueryHandler,
    ChatMemberHandler,
    filters,
    AIORateLimiter,
)

from utils.llm import build_tarot, build_numerology, llm_queue_size
import db
from db import (
    get_credits,
    add_credits,
    dec_credit,
    set_referral,
    event_count,
    all_user_ids,
    log_event,
    count_new_users,
    event_stats,
    unique_event_users,
    unique_event_users_like,
    get_menu_msg,
    set_menu_msg,
    is_new_user,
    referral_counts,
    add_alias,
    alias_owner,
    aliases_for,
    log_payment,
    referral_payment_stats,
    referral_payment_stats_user,
    payment_stats,
    payment_stats_since,
    unique_payers,
    referral_count,
    log_ab_event,
    set_subscribed,
)

db.init_sub()

# ── Логи ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)
START_TIME = dt.datetime.now()

# ── ENV ──────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YK_TOKEN = os.getenv("YK_TOKEN")
CACHE_CHAT_ID = int(os.getenv("CACHE_CHAT_ID", "0")) or None
SUPPORT_URL = "https://t.me/obotizatciaSupp_bot"
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x}

# ── Subscription ─────────────────────────────────────
# Проверяем подписку на канал при каждом обращении
CHANNEL_ID = -1002883355776
JOIN_URL = "https://t.me/obotization/3"

# ── Текст ────────────────────────────────────────────
GREETING = (
    "👋 Здравствуй, путник! Я к твоим услугам.\n"
    "🔢 Провожу нумерологический анализ.\n"
    "🗓️ Укажи дату рождения и узнай скрытый потенциал.\n"
    "💡 Расскажу о сильных и слабых сторонах.\n"
    "🌟 Поделюсь прогнозом на будущее.\n"
    "\n"
    "🃏 Делаю расклад Таро по выбранным картам.\n"
    "🔮 Толкую выпавшие арканы и даю совет.\n"
    "📈 Помогаю разобраться в ситуации.\n"
    "✨ Показываю возможные пути развития.\n"
    "_Первый анализ бесплатен._ Пригласи друга и получи +2 балла."
)
HOW_IT_WORKS = (
    "*Как это работает?*\n"
    "🔢 Нумерология раскрывает потенциал по дате рождения.\n"
    "✨ Цифры подсказывают сильные и слабые стороны.\n"
    "🚀 Я анализирую сочетания и даю прогноз.\n"
    "💡 Получаешь практичные рекомендации.\n"
    "\n"
    "🃏 Таро отражает текущие энергии выбранных карт.\n"
    "⚡️ Арканы показывают скрытые влияния и пути решения.\n"
    "🎯 Я читаю расклад и подсказываю действия.\n"
    "✨ Показываю возможные пути развития.\n"
    "🏁 Итог помогает выбрать верное направление."
)
INTRO_NUM = {
    "Карма": "Карма: отношения для отработки уроков.",
    "Судьба": "Судьба: предначертанный союз.",
    "Зеркало": "Зеркало: партнёр отражает твои качества.",
}
FOOTER_NUM = (
    "\n\n"
    "Это лишь одна грань твоей истории. В других сферах жизни тебя"
    " ещё ждёт много открытий. Пара кликов — и ты узнаешь больше о"
    " работе, саморазвитии или общем анализе."
)

FOOTER_TAROT = (
    "\n\n"
    "Голова выше, плечи назад. Только ты кузнец своего счастья, "
    "у тебя всё получится. Я в тебя верю."
)
REMINDER_TXT1 = "Привет, ну как сработал ли твой прогноз?"
REMINDER_TXT2 = "Ну что, проверим, что ждёт тебя сегодня?"

# Цены пакетов (в копейках) с учётом пониженного тарифа
PACKS_RUB = {3: 15_00, 8: 40_00, 15: 65_00, 40: 100_00}
# Стоимость пакетов в звёздах (1 ⭐ ≈ 1.79 ₽)
PACKS_STARS = {3: 8, 8: 22, 15: 36, 40: 56}
BONUS_REF = 2

# Стоимость услуг в баллах
COST_NUM = 2
COST_SPREAD = {1: 1, 3: 2, 5: 3}


def plural_ball(n: int) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return "баллов"
    n = n % 10
    if n == 1:
        return "балл"
    if 1 < n < 5:
        return "балла"
    return "баллов"
REMIND1_SEC = 24 * 3600
REMIND2_SEC = 72 * 3600
TG_LIMIT = 4096


# ── Файлы карт ───────────────────────────────────────
ASSETS_DIR = Path(__file__).resolve().parent
CARDS_DIR = ASSETS_DIR / "taro_cards"
BACK_PATH = ASSETS_DIR / "Card_back.png"
CARDS_JSON = ASSETS_DIR / "cards.json"

NUM_MAP = {
    1: "Туз",
    2: "Двойка",
    3: "Тройка",
    4: "Четвёрка",
    5: "Пятёрка",
    6: "Шестёрка",
    7: "Семёрка",
    8: "Восьмёрка",
    9: "Девятка",
    10: "Десятка",
    11: "Паж",
    12: "Рыцарь",
    13: "Королева",
    14: "Король",
}

SUIT_MAP = {
    "Wands": "Жезлов",
    "Cups": "Кубков",
    "Swords": "Мечей",
    "Pentacles": "Пентаклей",
}

MAJOR_MAP = {
    "The Fool": "Шут",
    "The Magician": "Маг",
    "The High Priestess": "Верховная Жрица",
    "The Empress": "Императрица",
    "The Emperor": "Император",
    "The Hierophant": "Иерофант",
    "The Lovers": "Влюблённые",
    "The Chariot": "Колесница",
    "Strength": "Сила",
    "The Hermit": "Отшельник",
    "Wheel of Fortune": "Колесо Фортуны",
    "Justice": "Справедливость",
    "The Hanged Man": "Повешенный",
    "Death": "Смерть",
    "Temperance": "Умеренность",
    "The Devil": "Дьявол",
    "The Tower": "Башня",
    "The Star": "Звезда",
    "The Moon": "Луна",
    "The Sun": "Солнце",
    "Judgement": "Суд",
    "The World": "Мир",
}

def _name_from_filename(fn: Path) -> tuple[str, str]:
    stem = fn.stem
    if re.match(r"^\d+", stem):
        num, suit = stem.split(" ", 1)
        num = int(num)
        ru_num = NUM_MAP.get(num, str(num))
        ru_suit = SUIT_MAP.get(suit, suit)
        return stem.lower().replace(" ", "_"), f"{ru_num} {ru_suit}"
    return stem.lower().replace(" ", "_"), MAJOR_MAP.get(stem, stem)

def _load_cards_json():
    if CARDS_JSON.exists():
        with CARDS_JSON.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"cache_chat_id": None, "back_msg_id": None, "cards": []}

def _save_cards_json(data: dict):
    tmp = CARDS_JSON.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(CARDS_JSON)

def admin_only(func):
    async def wrapper(update, ctx, *args, **kwargs):
        uid = update.effective_user.id
        if uid not in ADMIN_IDS:
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper

meta = _load_cards_json()
ALL_CARDS = [{"id": c["id"], "name": c["name"]} for c in meta.get("cards", [])]

DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")

def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mnt, y = m.groups()
    try:
        dt.date(int(y), int(mnt), int(d))
    except ValueError:
        return None
    return f"{int(d):02d}.{int(mnt):02d}.{y}"

def _digital_root(dob: str) -> int:
    digits = [int(c) for c in re.findall(r"\d", dob)]
    n = sum(digits)
    while n > 9:
        n = sum(int(c) for c in str(n))
    return n

def compatibility_label(dob1: str, dob2: str) -> str:
    d1 = _digital_root(dob1)
    d2 = _digital_root(dob2)
    s = (d1 + d2) % 3
    if s == 0:
        return "Зеркало"
    elif s == 1:
        return "Карма"
    return "Судьба"

def new_session_deck(uid: int, kind: str) -> list[dict]:
    import random, hashlib
    seed_text = f"{uid}:{dt.date.today().isoformat()}:{kind}"
    seed = int(hashlib.sha256(seed_text.encode()).hexdigest(), 16) % (10**8)
    rnd = random.Random(seed)
    meta = _load_cards_json()
    deck = [
        {"id": c["id"], "name": c["name"]}
        for c in meta.get("cards", [])
        if c.get("msg_id")
    ]
    rnd.shuffle(deck)
    return deck


def pick_options(deck: list[dict], picked: set[str], k: int) -> list[dict]:
    candidates = [c for c in deck if c["id"] not in picked]
    return candidates[:k]


class TypingCtx:
    """Контекстный менеджер, поддерживающий статус ``typing``."""

    def __init__(self, bot, chat_id, interval=4):
        self.bot = bot
        self.chat_id = chat_id
        self.interval = interval
        self.task = None

    async def _run(self):
        try:
            while True:
                await self.bot.send_chat_action(self.chat_id, "typing")
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run())

    async def __aexit__(self, exc_type, exc, tb):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


async def show_main(update: Update, ctx, uid: int | None = None, *, delete_prev: bool = True):
    uid = uid or update.effective_user.id
    credits = get_credits(uid, ADMIN_IDS)
    text = f"{GREETING}\n\nДоступно: *{credits}* {plural_ball(credits)}."
    m = await show(text, update, ctx, kb_main(uid, credits), delete_prev=delete_prev)
    old = get_menu_msg(uid)
    if old and old != m.message_id:
        with contextlib.suppress(BadRequest):
            await ctx.bot.delete_message(m.chat_id, old)
    set_menu_msg(uid, m.message_id)
    return m


# ── Клавиатуры ───────────────────────────────────────
def kb_main(uid: int, credits: int) -> Mk:
    return Mk(
        [
            [Btn("🔢 Нумерология", callback_data="num")],
            [Btn("🃏 Таро", callback_data="tarot")],
            [Btn("📑 Как это работает?", callback_data="how")],
            [Btn(f"💳 Пополнить баланс ({credits} {plural_ball(credits)})", callback_data="pay")],
            [Btn("👥 Пригласи друга", callback_data="ref")],
            [Btn("🛟 Техподдержка", url=SUPPORT_URL)],
        ]
    )


def kb_back():
    return Mk([[Btn("← Назад", callback_data="back")]])


def kb_ref(link: str) -> Mk:
    return Mk(
        [
            [Btn("📋 Скопировать ссылку", copy_text=CopyTextButton(link))],
            [Btn("📤 Отправить другу", url=f"https://t.me/share/url?url={link}")],
            [Btn("◀️ Назад", callback_data="back")],
        ]
    )


def kb_home():
    return Mk([[Btn("↩️ В меню", callback_data="home")]])


def kb_pay():
    return Mk(
        [
            [
                Btn("3 балла – 15 ₽", callback_data="buy_3_card"),
                Btn("3 балла – 8 ⭐", callback_data="buy_3_star"),
            ],
            [
                Btn("8 баллов – 40 ₽", callback_data="buy_8_card"),
                Btn("8 баллов – 22 ⭐", callback_data="buy_8_star"),
            ],
            [
                Btn("15 баллов – 65 ₽", callback_data="buy_15_card"),
                Btn("15 баллов – 36 ⭐", callback_data="buy_15_star"),
            ],
            [
                Btn("40 баллов – 100 ₽", callback_data="buy_40_card"),
                Btn("40 баллов – 56 ⭐", callback_data="buy_40_star"),
            ],
            [Btn("← Назад", callback_data="back")],
        ]
    )


def kb_topup(credits: int = 0):
    return Mk(
        [[Btn(f"💳 Пополнить баланс ({credits} {plural_ball(credits)})", callback_data="pay")]]
    )


def kb_gender():
    return Mk(
        [
            [Btn("Мужчина", callback_data="gender_m"), Btn("Женщина", callback_data="gender_f")],
            [Btn("← Назад", callback_data="back")],
        ]
    )


def kb_topic():
    return Mk(
        [
            [Btn("Отношения", callback_data="topic_rel")],
            [Btn("Работа", callback_data="topic_work")],
            [Btn("Саморазвитие", callback_data="topic_self")],
            [Btn("Семья", callback_data="topic_family")],
            [Btn("Общий анализ", callback_data="topic_general")],
            [Btn("← Назад", callback_data="back")],
        ]
    )


def kb_spread():
    return Mk(
        [
            [Btn("(1 балл) 1 карта", callback_data="spread_1")],
            [Btn("(2 балла) 3 карты", callback_data="spread_3")],
            [Btn("(3 балла) 5 карт", callback_data="spread_5")],
            [Btn("← Назад", callback_data="back")],
        ]
    )


def kb_cards(options: list[dict], step: int) -> Mk:
    rows = []
    for c in options:
        rows.append([Btn("🂠", callback_data=f"choose:{c['id']}:{step}")])
    rows.append([Btn("↩️ В меню", callback_data="back")])
    return Mk(rows)


def kb_sub():
    return Mk(
        [
            [Btn("✅ Подписаться", url=JOIN_URL)],
            [Btn("🔁 Проверить подписку", callback_data="chk_sub")],
        ]
    )


def split_chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, cur = [], []
    cur_len = 0
    for para in text.split("\n\n"):
        if cur_len + len(para) + 2 > limit:
            parts.append("\n\n".join(cur))
            cur, cur_len = [para], len(para)
        else:
            cur.append(para)
            cur_len += len(para) + 2
    if cur:
        parts.append("\n\n".join(cur))
    return parts


async def send_plain(chat, text: str, **kwargs):
    # никаких parse_mode для текста модели
    for chunk in split_chunks(text):
        await chat.send_message(chunk, **kwargs)


async def safe_send_report(chat, text_md: str, kb):
    clean = re.sub(r"[#*]", "", text_md)
    await send_plain(chat, clean, reply_markup=kb)


async def _check_member(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status in {"member", "administrator", "creator"}
    except (BadRequest, Forbidden):
        return False
    except Exception:
        return False


async def ensure_subscription(update: Update, ctx) -> bool:
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        return True
    ok = await _check_member(ctx.bot, uid)
    now = int(time.time())
    set_subscribed(uid, ok, now)
    if ok:
        return True
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_chat.send_message(
        "Чтобы пользоваться ботом, подпишись на наш канал. После подписки нажми «Проверить подписку» ниже.",
        reply_markup=kb_sub(),
    )
    return False


# ── helper: активное меню ───────────────────────────
async def set_active_menu(ctx, chat_id: int, message_id: int):
    old = ctx.user_data.pop("menu_id", None)
    if old and old != message_id:
        with contextlib.suppress(BadRequest):
            await ctx.bot.delete_message(chat_id, old)
    ctx.user_data["menu_id"] = message_id


# ── helper: вывод меню / редактирование ─────────────
async def show(
    text: str,
    update: Update,
    ctx,
    kb: Mk | None = None,
    pm="Markdown",
    *,
    menu=True,
    ttl: int = 0,
    delete_prev: bool = True,
) -> Message:
    if update.callback_query and delete_prev:
        q = update.callback_query
        try:
            m = await q.message.edit_text(text, reply_markup=kb, parse_mode=pm)
        except BadRequest:
            m = await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=pm)
    else:
        m = await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=pm)

    if menu:
        await set_active_menu(ctx, m.chat_id, m.message_id)
    else:
        ctx.user_data.pop("menu_id", None)

    if ttl:

        async def _delete(c):
            try:
                await c.bot.delete_message(m.chat_id, m.message_id)
            except BadRequest:
                pass

        ctx.job_queue.run_once(_delete, ttl, chat_id=m.chat_id)

    return m


# ── /start ───────────────────────────────────────────
async def start(update: Update, ctx):
    if not await ensure_subscription(update, ctx):
        return
    uid = update.effective_user.id
    args = update.message.text.split(maxsplit=1)[1:] if update.message else []

    first_visit = is_new_user(uid)

    referred = False
    if args:
        ref = args[0]
        parent = int(ref) if ref.isdigit() else alias_owner(ref)
        if parent and set_referral(uid, parent):
            referred = True
            add_credits(parent, BONUS_REF)
            try:
                name = html.escape(update.effective_user.full_name)
                link = f'<a href="tg://user?id={uid}">{name}</a>'
                total = get_credits(parent, ADMIN_IDS)
                await ctx.bot.send_message(
                    parent,
                    f"🎉 {link} (ID {uid}) присоединился по твоей ссылке!\n"
                    f"+{BONUS_REF} {plural_ball(BONUS_REF)} начислено. Теперь доступно: {total} {plural_ball(total)}.",
                    parse_mode=ParseMode.HTML,
                )
            except NetworkError:
                pass

    log_event("start", uid)

    # 1-й визит → автоматически +2, по реферальной ссылке +4
    if first_visit:
        add_credits(uid, 4 if referred else 2)
        set_subscribed(uid, True, int(time.time()))

    await show_main(update, ctx, uid)


# ── Router ──────────────────────────────────────────
async def router(update: Update, ctx):
    q = update.callback_query
    data = q.data
    if data == "chk_sub":
        uid = update.effective_user.id
        ok = await _check_member(ctx.bot, uid)
        now_ts = int(time.time())
        set_subscribed(uid, ok, now_ts)
        if ok:
            await q.answer()
            with contextlib.suppress(BadRequest):
                await q.message.edit_reply_markup(reply_markup=None)
            await q.message.reply_text("Подписка подтверждена. Продолжаем!")
            await show_main(update, ctx, uid)
        else:
            await q.answer(
                "Подписка пока не найдена. Проверь, что ты подписан/а, и нажми ещё раз.",
                show_alert=True,
            )
        return
    if not await ensure_subscription(update, ctx):
        return
    await q.answer()
    uid = update.effective_user.id
    now = asyncio.get_running_loop().time()
    last = ctx.user_data.get("last_cb")
    if last and (q.id == last[0] or (data == last[1] and now - last[2] < 0.3)):
        return
    ctx.user_data["last_cb"] = (q.id, data, now)
    log_event(data, uid)
    credits = get_credits(uid, ADMIN_IDS)

    if data == "num":
        if credits < COST_NUM:
            await show(
                "❌ Недостаточно баллов. Пополни баланс.",
                update,
                ctx,
                kb_topup(credits),
                menu=False,
            )
            return
        ctx.user_data.clear()
        ctx.user_data["mode"] = "num_dob1"
        msg = (
            f"Стоимость: {COST_NUM} {plural_ball(COST_NUM)}.\n"
            "Укажи свою дату рождения (ДД.ММ.ГГГГ)"
        )
        m = await show(msg, update, ctx, kb_back(), menu=False)
        ctx.user_data["prompt_id"] = m.message_id
    elif data == "tarot":
        if credits < 1:
            await show(
                "❌ Недостаточно баллов. Пополни баланс.",
                update,
                ctx,
                kb_topup(credits),
                menu=False,
            )
            return
        ctx.user_data.clear()
        ctx.user_data["mode"] = "tarot_spread"
        await show(
            "Выбери расклад:\n"
            "🃏 1 карта — карта дня/совет\n\n"
            "🃏🃏🃏 3 карты — прошлое → настоящее → будущее\n\n"
            "🃏🃏🃏🃏🃏 5 карт — ситуация → узкое место → совет → ресурс → итог",
            update,
            ctx,
            kb_spread(),
        )
    elif data.startswith("spread_"):
        spread = int(data.split("_", 1)[1])
        cost = COST_SPREAD[spread]
        if credits < cost:
            await show(
                "❌ Недостаточно баллов. Пополни баланс.",
                update,
                ctx,
                kb_topup(credits),
                menu=False,
            )
            return
        ctx.user_data["mode"] = "tarot_pick"
        ctx.user_data["spread"] = spread
        ctx.user_data["deck"] = new_session_deck(uid, str(spread))
        ctx.user_data["picked"] = []
        ctx.user_data["step"] = 0
        try:
            await q.message.delete()
        except BadRequest:
            pass
        options = pick_options(ctx.user_data["deck"], set(), {1:6,3:5,5:4}[spread])
        meta = _load_cards_json()
        cache_id = meta.get("cache_chat_id")
        back_msg = meta.get("back_msg_id")
        if cache_id and back_msg:
            sent = await ctx.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=cache_id,
                message_id=back_msg,
                caption="Выбери карту:",
                reply_markup=kb_cards(options, 0),
            )
        else:
            sent = await show("Выбери карту:", update, ctx, kb_cards(options, 0), menu=False)
        ctx.user_data["last_card_msg_id"] = sent.message_id
    elif data.startswith("choose:"):
        _, cid, step = data.split(":")
        step = int(step)
        last_msg = ctx.user_data.get("last_card_msg_id")
        if last_msg:
            with contextlib.suppress(BadRequest):
                await ctx.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id,
                    message_id=last_msg,
                    reply_markup=None,
                )
        ctx.user_data["picked"].append(cid)
        spread = ctx.user_data["spread"]
        deck = ctx.user_data["deck"]
        meta = _load_cards_json()
        cache_id = meta.get("cache_chat_id")
        card = next(
            (c for c in meta.get("cards", []) if c["id"] == cid and c.get("msg_id")),
            None,
        )
        # не удаляем выбранные карты, чтобы пользователь видел весь расклад
        if step + 1 >= spread:
            reply_markup = None
        else:
            options = pick_options(deck, set(ctx.user_data["picked"]), {1:6,3:5,5:4}[spread])
            reply_markup = kb_cards(options, step + 1)
        if cache_id and card and card.get("msg_id"):
            sent = await ctx.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=cache_id,
                message_id=card["msg_id"],
                caption=f"Ты выбрал: {card['name']}",
                reply_markup=reply_markup,
            )
        else:
            sent = await update.effective_chat.send_message(
                f"Ты выбрал: {card['name'] if card else cid}", reply_markup=reply_markup
            )
        if reply_markup:
            ctx.user_data["last_card_msg_id"] = sent.message_id
        else:
            ctx.user_data.pop("last_card_msg_id", None)

        if step + 1 >= spread:
            proc = await update.effective_chat.send_message("⌛ Твой расклад готовится, подожди...")
            cards = [next(c for c in deck if c["id"] == x) for x in ctx.user_data["picked"]]
            dob = ctx.user_data.get("dob1")
            gender = ctx.user_data.get("gender")
            loop = asyncio.get_running_loop()
            variant = "B" if uid % 2 else "A"
            report = await loop.run_in_executor(None, build_tarot, cards, spread, dob, gender, variant)
            log_event(f"tarot{variant}", uid)
            log_ab_event(uid, "tarot", variant, len(report))
            ctx.user_data.clear()
            with contextlib.suppress(BadRequest):
                await proc.delete()

            try:
                await safe_send_report(
                    update.effective_chat,
                    report + FOOTER_TAROT,
                    kb_home(),
                )
            except Exception:
                await show(
                    "⚠️ Временно нет связи. Попробуйте ещё раз позже.",
                    update,
                    ctx,
                    kb_home(),
                    menu=False,
                )
                return

            dec_credit(uid, ADMIN_IDS, COST_SPREAD[spread])
            schedule_reminders(ctx, uid)
        else:
            ctx.user_data["step"] = step + 1
    elif data.startswith("gender_"):
        ctx.user_data["gender"] = "мужчина" if data.endswith("m") else "женщина"
        pid = ctx.user_data.pop("prompt_id", None)
        if pid:
            try:
                await ctx.bot.delete_message(update.effective_chat.id, pid)
            except BadRequest:
                pass
        if ctx.user_data.get("mode") == "num_gender":
            ctx.user_data["mode"] = "num_topic"
            m = await show("Выбери область:", update, ctx, kb_topic())
            ctx.user_data["prompt_id"] = m.message_id
    elif data.startswith("topic_"):
        topic_map = {
            "rel": "Отношения",
            "work": "Работа",
            "self": "Саморазвитие",
            "family": "Семья",
            "general": "Общий анализ",
        }
        ctx.user_data["topic"] = topic_map.get(data.split("_",1)[1])
        pid = ctx.user_data.pop("prompt_id", None)
        if pid:
            try:
                await ctx.bot.delete_message(update.effective_chat.id, pid)
            except BadRequest:
                pass
        if ctx.user_data["topic"] in ("Отношения", "Семья"):
            ctx.user_data["mode"] = "num_dob2"
            if ctx.user_data["topic"] == "Отношения":
                txt = "Введи дату рождения второй половинки (ДД.ММ.ГГГГ)"
            else:
                txt = "Введи дату рождения своего члена семьи (ДД.ММ.ГГГГ)"
            m = await show(txt, update, ctx, kb_back(), menu=False)
            ctx.user_data["prompt_id"] = m.message_id
        else:
            await send_numerology(update, ctx, uid)
    elif data == "how":
        await show(HOW_IT_WORKS, update, ctx, kb_back())
    elif data == "pay":
        await show("💳 *Выбери пакет:*", update, ctx, kb_pay())
    elif data == "ref":
        names = aliases_for(uid)
        code = names[0] if names else str(uid)
        link = f"https://t.me/{ctx.bot.username}?start={code}"
        await show(
            "Если вдруг у тебя закончились баллы, не переживай!\n"
            "Предложи этого бота друзьям и получи дополнительные баллы.\n"
            f"🔗 Твоя ссылка: `{link}`\n"
            f"За каждого нового пользователя — *+{BONUS_REF} {plural_ball(BONUS_REF)}*.\n"
            f"За каждого приглашённого пользователя ты получаешь {BONUS_REF} {plural_ball(BONUS_REF)}.",
            update,
            ctx,
            kb_ref(link),
        )
    elif data == "home":
        with contextlib.suppress(BadRequest):
            await q.message.edit_reply_markup(None)
        ctx.user_data.clear()
        await show_main(update, ctx, uid, delete_prev=False)
    elif data == "back":
        ctx.user_data.clear()
        await show_main(update, ctx, uid)
    elif data.startswith("buy_"):
        _, qty, method = data.split("_")
        qty = int(qty)
        if method == "card":
            token = YK_TOKEN
            currency = "RUB"
            price = PACKS_RUB[qty]
        else:
            token = ""
            currency = "XTR"
            price = PACKS_STARS[qty]

        kb = Mk(
            [
                [Btn("Оплатить", pay=True)],
                [Btn("← Назад", callback_data="back_pay")],
            ]
        )

        inv = await ctx.bot.send_invoice(
            chat_id=q.message.chat_id,
            title=f"{qty} {plural_ball(qty)}",
            description="Пакет баллов",
            payload=f"buy_{qty}_{method}",
            provider_token=token,
            currency=currency,
            prices=[LabeledPrice(f"{qty} {plural_ball(qty)}", price)],
            reply_markup=kb,
        )
        ctx.user_data["inv_msg"] = inv.message_id
        old = ctx.user_data.pop("menu_id", None)
        if old and old != inv.message_id:
            try:
                await ctx.bot.delete_message(q.message.chat_id, old)
            except BadRequest:
                pass
        ctx.user_data["menu_id"] = inv.message_id
    elif data == "back_pay":
        try:
            await q.message.delete()
        except BadRequest:
            pass
        await show("💳 *Выбери пакет:*", update, ctx, kb_pay())


async def send_numerology(update: Update, ctx, uid: int):
    if not await ensure_subscription(update, ctx):
        return
    dob1 = ctx.user_data.pop("dob1")
    dob2 = ctx.user_data.pop("dob2", None)
    gender = ctx.user_data.pop("gender", None)
    topic = ctx.user_data.pop("topic", None)
    proc = await update.effective_chat.send_message(
        "⌛ (Твой запрос обрабатывается, подожди…подготовка ответа может занять до минуты)"
    )
    loop = asyncio.get_running_loop()
    variant = "B" if uid % 2 else "A"
    report = await loop.run_in_executor(None, build_numerology, dob1, gender, topic, dob2, variant)
    label = compatibility_label(dob1, dob2) if dob2 else None
    log_event(f"num{variant}", uid)
    log_ab_event(uid, "num", variant, len(report))

    # Убираем "обрабатывается"
    with contextlib.suppress(BadRequest):
        await proc.delete()

    # 1) Пытаемся доставить
    try:
        intro = INTRO_NUM.get(label, "")
        await safe_send_report(
            update.effective_chat,
            (intro + "\n\n" if intro else "") + report + FOOTER_NUM,
            kb_home(),
        )
    except Exception:
        # 2) Если не ушло — покажем вежливую плашку и выходим БЕЗ списания
        await show(
            "⚠️ Временно нет связи. Попробуйте ещё раз позже.",
            update,
            ctx,
            kb_home(),
            menu=False,
        )
        return

    # 3) Доставили успешно — теперь списываем
    dec_credit(uid, ADMIN_IDS, COST_NUM)
    schedule_reminders(ctx, uid)


async def handle_text(update: Update, ctx):
    if not await ensure_subscription(update, ctx):
        return
    mode = ctx.user_data.get("mode")
    if mode == "num_dob1":
        dob = parse_date(update.message.text)
        if not dob:
            await update.message.reply_text("Введи дату в формате ДД.ММ.ГГГГ")
            return
        pid = ctx.user_data.pop("prompt_id", None)
        if pid:
            try:
                await ctx.bot.delete_message(update.effective_chat.id, pid)
            except BadRequest:
                pass
        ctx.user_data["dob1"] = dob
        ctx.user_data["mode"] = "num_gender"
        m = await update.message.reply_text("Укажи пол:", reply_markup=kb_gender())
        ctx.user_data["prompt_id"] = m.message_id
    elif mode == "num_dob2":
        dob = parse_date(update.message.text)
        if not dob:
            await update.message.reply_text("Введи дату в формате ДД.ММ.ГГГГ")
            return
        pid = ctx.user_data.pop("prompt_id", None)
        if pid:
            try:
                await ctx.bot.delete_message(update.effective_chat.id, pid)
            except BadRequest:
                pass
        ctx.user_data["dob2"] = dob
        await send_numerology(update, ctx, update.effective_user.id)
    else:
        await show_main(update, ctx, update.effective_user.id)


# ── Payment ─────────────────────────────────────────
async def pre_checkout(update: Update, ctx):
    if not await ensure_subscription(update, ctx):
        await update.pre_checkout_query.answer(
            False, "Подпишись на канал и попробуй снова"
        )
        return
    await update.pre_checkout_query.answer(True)


async def paid(update: Update, ctx):
    if not await ensure_subscription(update, ctx):
        return
    parts = update.message.successful_payment.invoice_payload.split("_")
    qty = int(parts[1])
    method = parts[2] if len(parts) > 2 else "card"
    uid = update.effective_user.id
    charge_id = update.message.successful_payment.provider_payment_charge_id
    log_event("paid", uid)
    if log_payment(uid, qty, method, charge_id):
        add_credits(uid, qty)
    credits = get_credits(uid, ADMIN_IDS)
    # удалить счёт
    inv_id = ctx.user_data.pop("inv_msg", None)
    if inv_id:
        try:
            await ctx.bot.delete_message(uid, inv_id)
        except BadRequest:
            pass
    await show(
        f"✅ Оплата прошла!\n\nДоступно: *{credits}* {plural_ball(credits)}.\n",
        update,
        ctx,
        kb_main(uid, credits),
        menu=False,
        ttl=5,
    )
    await show_main(update, ctx, uid)




async def reminder1(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.chat_id
    credits = get_credits(uid, ADMIN_IDS)
    if credits > 0:
        try:
            await ctx.bot.send_message(uid, REMINDER_TXT1)
        except BadRequest:
            pass

async def reminder2(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.chat_id
    credits = get_credits(uid, ADMIN_IDS)
    if credits > 0:
        try:
            await ctx.bot.send_message(uid, REMINDER_TXT2)
        except BadRequest:
            pass

async def my_chat_member(update: Update, ctx):
    st = update.my_chat_member.new_chat_member.status
    if st in {"kicked", "left"}:
        log_event("bot_removed", update.effective_user.id)

def schedule_reminders(ctx, uid):
    for name in (f"rem1_{uid}", f"rem2_{uid}"):
        for job in ctx.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    ctx.job_queue.run_once(reminder1, REMIND1_SEC, chat_id=uid, name=f"rem1_{uid}")
    ctx.job_queue.run_once(reminder2, REMIND2_SEC, chat_id=uid, name=f"rem2_{uid}")


async def ping(update: Update, ctx):
    await update.message.reply_text("pong")


@admin_only
async def health(update: Update, ctx):
    uptime = dt.datetime.now() - START_TIME
    tasks = len(asyncio.all_tasks())
    llm = llm_queue_size()
    try:
        ver = subprocess.check_output(["git", "describe", "--always"]).decode().strip()
    except Exception:
        ver = "unknown"
    text = f"uptime: {uptime}\ntasks: {tasks}\nllm queue: {llm}\nversion: {ver}"
    await update.message.reply_text(text)




# ── /grant ──────────────────────────────────────────
async def grant(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(ctx.args) not in (1, 2):
        await update.message.reply_text("Usage: /grant <qty> | /grant <id> <qty>")
        return
    uid, qty = (
        (update.effective_user.id, int(ctx.args[0]))
        if len(ctx.args) == 1
        else (int(ctx.args[0]), int(ctx.args[1]))
    )
    add_credits(uid, qty)
    total = get_credits(uid, ADMIN_IDS)
    await update.message.reply_text(f"✅ Теперь {total}.")
    if uid != update.effective_user.id:
        try:
            await ctx.bot.send_message(
                uid,
                f"🎁 Тебе начислено {qty} {plural_ball(qty)} от администратора.\nДоступно: {total} {plural_ball(total)}.",
            )
        except BadRequest:
            pass


async def broadcast(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <text>")
        return
    text = " ".join(ctx.args)
    for uid in all_user_ids():
        try:
            await ctx.bot.send_message(uid, text)
        except (BadRequest, Forbidden):
            pass
        except Exception as e:
            log.warning(f"Broadcast to {uid} failed: {e}")
    await update.message.reply_text("✅ Рассылка завершена.")


async def set_alias_cmd(update: Update, ctx):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /alias <name>")
        return
    alias = ctx.args[0].lower()
    if not re.fullmatch(r"[a-zA-Z0-9_]+", alias):
        await update.message.reply_text("Недопустимое имя. Буквы, цифры и _")
        return
    if add_alias(uid, alias):
        link = f"https://t.me/{ctx.bot.username}?start={alias}"
        await update.message.reply_text(
            f"🔗 Твоя ссылка: {link}", reply_markup=kb_ref(link)
        )
    else:
        await update.message.reply_text("❌ Имя занято.")


async def profile(update: Update, ctx):
    uid = update.effective_user.id
    name = html.escape(update.effective_user.full_name)
    credits = get_credits(uid, ADMIN_IDS)
    answers = (
        event_count(uid, "numA")
        + event_count(uid, "numB")
        + event_count(uid, "tarotA")
        + event_count(uid, "tarotB")
    )
    refs = referral_count(uid)
    names = aliases_for(uid)
    code = names[0] if names else str(uid)
    link = f"https://t.me/{ctx.bot.username}?start={code}"
    kb = Mk([[Btn("Скопировать", switch_inline_query=link)]])
    text = (
        f"<b>{name}</b>\n"
        f"Баланс: {credits} {plural_ball(credits)}\n"
        f"Ответов получено: {answers}\n"
        f"Привёл друзей: {refs}\n"
        f"<code>{link}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def my_referrals(update: Update, ctx):
    uid = update.effective_user.id
    if not aliases_for(uid):
        await update.message.reply_text("Сначала создай реферальное имя командой /alias")
        return
    refs = referral_count(uid)
    payments = referral_payment_stats_user(uid)
    rub = (
        sum(PACKS_RUB.get(q, 0) * c for m, q, c in payments if m == "card") // 100
    )
    star = sum(PACKS_STARS.get(q, 0) * c for m, q, c in payments if m != "card")
    text = f"Пришло: {refs}\nОплачено: {rub} ₽, {star} ⭐"
    await update.message.reply_text(text)


async def stats(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    day1 = count_new_users(1)
    day3 = count_new_users(3)
    week = count_new_users(7)
    month = count_new_users(30)
    events = event_stats()
    num_cnt = events.get("numA", 0) + events.get("numB", 0)
    tarot_cnt = events.get("tarotA", 0) + events.get("tarotB", 0)
    num_users = unique_event_users_like("num")
    tarot_users = unique_event_users_like("tarot")
    total_checks = num_cnt + tarot_cnt
    pay_visits = unique_event_users("pay")
    pay_clicks = unique_event_users_like("buy_")
    pay_backs = unique_event_users("back_pay")
    payments = payment_stats()
    payments30 = payment_stats_since(30)
    pay_users = unique_payers()
    card_packs: dict[int, int] = {}
    star_packs: dict[int, int] = {}
    for method, qty, cnt in payments:
        if method == "card":
            card_packs[qty] = card_packs.get(qty, 0) + cnt
        else:
            star_packs[qty] = star_packs.get(qty, 0) + cnt
    card_packs30: dict[int, int] = {}
    star_packs30: dict[int, int] = {}
    for method, qty, cnt in payments30:
        if method == "card":
            card_packs30[qty] = card_packs30.get(qty, 0) + cnt
        else:
            star_packs30[qty] = star_packs30.get(qty, 0) + cnt
    cnt_card = sum(card_packs.values())
    cnt_star = sum(star_packs.values())
    total_rub = sum(PACKS_RUB.get(q, 0) * c for q, c in card_packs.items()) // 100
    total_star = sum(PACKS_STARS.get(q, 0) * c for q, c in star_packs.items())
    total_rub30 = (
        sum(PACKS_RUB.get(q, 0) * c for q, c in card_packs30.items()) // 100
    )
    total_star30 = sum(
        PACKS_STARS.get(q, 0) * c for q, c in star_packs30.items()
    )
    top_method = "₽" if cnt_card >= cnt_star else "⭐"
    refs = referral_counts()
    total_refs = sum(c for _, c in refs)
    topics = {
        "Отношения": events.get("topic_rel", 0),
        "Работа": events.get("topic_work", 0),
        "Саморазвитие": events.get("topic_self", 0),
        "Семья": events.get("topic_family", 0),
        "Общий анализ": events.get("topic_general", 0),
    }

    card_details = ", ".join(f"{q}×{c}" for q, c in sorted(card_packs.items()))
    star_details = ", ".join(f"{q}×{c}" for q, c in sorted(star_packs.items()))

    rows = [
        ("Новых за 1д", day1),
        ("за 3д", day3),
        ("за 7д", week),
        ("за 30д", month),
        ("Перешли к оплате", pay_visits),
        ("Нажали оплатить", pay_clicks),
        ("Назад из оплаты", pay_backs),
        ("Оплатили", pay_users),
        ("Сумма ₽", total_rub),
        ("Сумма ⭐", total_star),
        ("Сумма ₽ 30д", total_rub30),
        ("Сумма ⭐ 30д", total_star30),
        ("Популярный метод", top_method),
        ("Нумерология", f"{num_users}/{num_cnt}"),
        ("Таро", f"{tarot_users}/{tarot_cnt}"),
        ("Всего проверок", total_checks),
        ("Приглашено", total_refs),
    ]

    for name, cnt in topics.items():
        rows.append((f"Тема {name}", cnt))
    if card_details:
        rows.append(("Пакеты ₽", card_details))
    if star_details:
        rows.append(("Пакеты ⭐", star_details))

    table = ["|Показатель|Кол.|", "|---|---:|"]
    for k, v in rows:
        table.append(f"|{k}|{v}|")
    html_table = "<b>Статистика</b>\n<pre>\n" + "\n".join(table) + "\n</pre>"
    await update.message.reply_text(html_table, parse_mode=ParseMode.HTML)


async def stats_referrals(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    refs = referral_counts()
    if not refs:
        await update.message.reply_text("Рефералов нет.")
        return
    payments = referral_payment_stats()
    rub = {}
    star = {}
    for uid, method, qty, cnt in payments:
        if method == "card":
            rub[uid] = rub.get(uid, 0) + PACKS_RUB.get(qty, 0) * cnt
        else:
            star[uid] = star.get(uid, 0) + PACKS_STARS.get(qty, 0) * cnt

    table = ["|ID|Имя|Кол.|₽|⭐|", "|---|---|---:|---:|---:|"]
    for uid, cnt in refs:
        names = ", ".join(aliases_for(uid)) or "-"
        link = f'<a href="tg://user?id={uid}">{uid}</a>'
        r = rub.get(uid, 0) // 100
        s = star.get(uid, 0)
        table.append(
            f"|{link}|{html.escape(names)}|{cnt}|{r}|{s}|"
        )

    text = "<b>Рефералы</b>\n<pre>\n" + "\n".join(table) + "\n</pre>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def ab_stats(update: Update, ctx):
    rows = db.DB.execute(
        "SELECT variant, COUNT(*), AVG(length), COUNT(DISTINCT uid) FROM ab_events GROUP BY variant"
    ).fetchall()
    data = {r[0]: {"count": r[1], "avg": int(r[2] or 0), "users": r[3]} for r in rows}
    firsts = db.DB.execute(
        "SELECT uid, variant, MIN(ts) FROM ab_events GROUP BY uid, variant"
    ).fetchall()
    retain = {"A": 0, "B": 0}
    paid = {"A": 0, "B": 0}
    for uid, var, ts in firsts:
        if db.DB.execute("SELECT 1 FROM events WHERE uid=? AND ts>=?", (uid, ts + 86400)).fetchone():
            retain[var] += 1
        if db.DB.execute("SELECT 1 FROM payments WHERE uid=? AND ts>=?", (uid, ts)).fetchone():
            paid[var] += 1
    table = ["|Ver|Users|AvgLen|Ret24|Paid|", "|---|---:|---:|---:|---:|"]
    for v in ("A", "B"):
        st = data.get(v, {})
        table.append(
            f"|{v}|{st.get('users',0)}|{st.get('avg',0)}|{retain.get(v,0)}|{paid.get(v,0)}|"
        )
    text = "<b>A/B статистика</b>\n<pre>\n" + "\n".join(table) + "\n</pre>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def cache_back(update: Update, ctx):
    """Отправить 'рубашку' в канал-кэш и запомнить message_id."""
    data = _load_cards_json()
    cache_id = CACHE_CHAT_ID or data.get("cache_chat_id")
    if not cache_id:
        await update.message.reply_text("Не задан CACHE_CHAT_ID")
        return
    if not BACK_PATH.exists():
        await update.message.reply_text(f"Не найден файл {BACK_PATH}")
        return
    with BACK_PATH.open("rb") as f:
        m = await ctx.bot.send_photo(cache_id, photo=f, caption="Рубашка")
    data["cache_chat_id"] = cache_id
    data["back_msg_id"] = m.message_id
    _save_cards_json(data)
    await update.message.reply_text("✅ Рубашка сохранена.")


@admin_only
async def cache_cards(update: Update, ctx):
    """Закачать все карты в канал-кэш и запомнить message_id."""
    if not CARDS_DIR.exists():
        await update.message.reply_text(f"Не найдена папка {CARDS_DIR}")
        return

    files = sorted([p for p in CARDS_DIR.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
    if not files:
        await update.message.reply_text("В папке нет изображений карт.")
        return

    data = _load_cards_json()
    cache_id = CACHE_CHAT_ID or data.get("cache_chat_id")
    if not cache_id:
        await update.message.reply_text("Не задан CACHE_CHAT_ID")
        return
    known = {c["id"] for c in data.get("cards", [])}
    added = 0

    await update.message.reply_text(
        f"Начинаю кэшировать {len(files)} файлов. Пожалуйста, не нажимай ничего."
    )

    for i, path in enumerate(files, 1):
        cid, name = _name_from_filename(path)
        if cid in known:
            continue
        try:
            with path.open("rb") as f:
                m = await ctx.bot.send_photo(cache_id, photo=f, caption=name)
            data.setdefault("cards", []).append({"id": cid, "name": name, "msg_id": m.message_id})
            added += 1
            if added % 5 == 0:
                _save_cards_json(data)
            await asyncio.sleep(0.6)
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            continue
        except TimedOut:
            await asyncio.sleep(1.0)
            continue

    _save_cards_json(data)
    total = len(data.get('cards', []))
    await update.message.reply_text(
        f"✅ Готово. Добавлено {added} карт. Всего в cards.json: {total}."
    )


@admin_only
async def cards_cleanup(update: Update, ctx):
    data = _load_cards_json()
    cards = data.get("cards", [])
    cards = [c for c in cards if c.get("msg_id")]
    seen = set()
    cleaned = []
    for c in cards:
        key = c["name"]
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(c)
    data["cards"] = cleaned
    _save_cards_json(data)
    await update.message.reply_text(f"🧹 Оставлено {len(cleaned)} карт с msg_id.")


@admin_only
async def cards_check(update, ctx):
    data = _load_cards_json()
    cache_id = data.get("cache_chat_id")
    bad = []
    for c in data.get("cards", []):
        try:
            await ctx.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=cache_id,
                message_id=c["msg_id"],
            )
            await asyncio.sleep(0.15)
        except Exception:
            bad.append(c["name"])
    if bad:
        await update.message.reply_text("❌ Проблемы с:\n" + "\n".join(bad))
    else:
        await update.message.reply_text("✅ Все карты копируются.")


# ── Error-лог ───────────────────────────────────────
async def err(update: object, ctx):
    err = ctx.error
    if isinstance(err, NetworkError):
        log.warning("Network error: %s", err)
        if update and getattr(update, "effective_chat", None):
            try:
                await ctx.bot.send_message(
                    update.effective_chat.id,
                    "⚠️ Временно нет связи. Попробуйте ещё раз позже.",
                )
            except NetworkError:
                pass
        return
    log.error("Exception:", exc_info=err)


# ── main ────────────────────────────────────────────
def main():
    req = HTTPXRequest(
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        connection_pool_size=1024,
        pool_timeout=10,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(64)
        .job_queue(JobQueue())
        .build()
    )

    app.add_handlers(
        [
            CommandHandler("start", start),
            CommandHandler("grant", grant),
            CommandHandler("broadcast", broadcast),
            CommandHandler("alias", set_alias_cmd),
            CommandHandler("profile", profile),
            CommandHandler("refs", my_referrals),
            CommandHandler("stats", stats),
            CommandHandler("stats_referrals", stats_referrals),
            CommandHandler("ab_stats", ab_stats),
            CommandHandler("ping", ping),
            CommandHandler("health", health),
            CommandHandler("cache_back", cache_back),
            CommandHandler("cache_cards", cache_cards),
            CommandHandler("cards_cleanup", cards_cleanup),
            CommandHandler("cards_check", cards_check),
            CallbackQueryHandler(router),
            PreCheckoutQueryHandler(pre_checkout),
            MessageHandler(filters.SUCCESSFUL_PAYMENT, paid),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        ]
    )
    app.add_error_handler(err)

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
