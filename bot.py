# bot.py – версия 02-06-2025: фиксы оплаты, 1-й бесплатный анализ, уведомление grant
# -*- coding: utf-8 -*-
import os, io, logging, asyncio, contextlib, re, html
from dotenv import load_dotenv
from PIL import Image
from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Mk,
    LabeledPrice,
    Message,
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.error import BadRequest, NetworkError
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
)

from utils.facepp import detect_attributes
from utils.llm import build_profile
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
)

# ── Логи ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YK_TOKEN = os.getenv("YK_TOKEN")
SUPPORT = os.getenv("SUPPORT", "@support")
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x}

# ── Текст ────────────────────────────────────────────
GREETING = (
    "👋 *Добро пожаловать в Лицемер.*\n"
    "1. Мы проводим научный физиогномический анализ прямо по вашей фотографии.\n"
    "Методика основана на проверенных исследованиях и подтверждённой статистике.\n"
    "Этот бот читает людей и выдает тебе инструкцию к достижению желаемой цели.\n"
    "—\n"
    "🔹 Найти подход к коллеге по работе 🤝\n"
    "🔹 Получить ключик к сердцу противоположного пола 🔑❤️\n"
    "🔹 Узнать получше клиента перед продажей 💼\n"
    "🔹 Провести общий анализ и свериться с тем, что вы и так знаете 🔍\n"
    "Выбирай цель, скидывай фото, получай гайд, всё просто, удачи!\n"
    "\n"
    "2. Проводит тарологический прогноз\n"
    "_Первый анализ бесплатен._ Пригласи друга по реферальной ссылке и получи +2 анализа."
)
HOW_IT_WORKS = (
    "*Как это работает?*\n"
    "📊 Физиогномика: фото обрабатывается физиогномическим анализом, базирующимся на научных исследованиях и данных с известной погрешностью из открытых источников.\n"
    "1. Фото обрабатывается и извлекаются ключевые физиогномические данные.\n"
    "2. На основе этих данных формируется точный профиль характера и темперамента.\n"
    "3. Готовый анализ сразу отправляется вам.\n"
    "—\n"
    "🃏 Тарологический прогноз:\n"
    "Мы считываем твою мимику, эмоции, состояние — и превращаем их в пять «карт»:\n"
    "архетипы из системы Таро, но без суеверий. Вместо колоды — ты сам.\n"
    "Твое лицо как маршрут на день: что внутри движется, что мешает, что поддерживает, и к чему зовёт твоя энергия прямо сейчас.\n\n"
    "🗝️ Это инструмент для понимания, не для предсказания.\n"
    "И пусть сегодняшний день начнётся с понимания того, кто ты есть именно сейчас.\n\n"
    "🔥 Пригласи друга → +2 анализа."
)
FOOTER = (
    "\n\n"
    "Голова выше, плечи назад. Только ты кузнец своего счастья, "
    "у тебя всё получится. Я в тебя верю.\n"
    "🗝️ Ты получил *чит-код действий*. Действуй!"
)

FOOTER_TAROT = (
    "\n\n"
    "Голова выше, плечи назад. Только ты кузнец своего счастья, "
    "у тебя всё получится. Я в тебя верю."
)
REMINDER_TXT1 = "Привет! Получилось ли достичь цели?"
REMINDER_TXT2 = (
    "Я уверен, у тебя всё получилось. Надеюсь, ты ещё не решил захватить мир, "
    "используя меня. Если понадобятся новые данные, пришли фото."
)

PACKS_RUB = {2: 100_00, 5: 200_00, 10: 389_00}
# Стоимость пакетов в звёздах
PACKS_STARS = {2: 55, 5: 110, 10: 220}
BONUS_REF = 2
REMIND1_SEC = 24 * 3600
REMIND2_SEC = 72 * 3600
SEM = asyncio.Semaphore(32)
PHOTO_TIMEOUT_SEC = 15 * 60


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


async def show_main(update: Update, ctx, uid: int | None = None):
    uid = uid or update.effective_user.id
    credits = get_credits(uid, ADMIN_IDS)
    text = f"{GREETING}\n\nДоступно: *{credits}* анализов."
    m = await show(text, update, ctx, kb_main(uid, credits))
    old = get_menu_msg(uid)
    if old and old != m.message_id and not update.callback_query:
        try:
            await ctx.bot.delete_message(m.chat_id, old)
        except BadRequest:
            pass
    set_menu_msg(uid, m.message_id)
    return m


# ── Клавиатуры ───────────────────────────────────────
def kb_main(uid: int, credits: int) -> Mk:
    return Mk(
        [
            [Btn("🔍 Физиогномический анализ", callback_data="scan")],
            [Btn("🃏 ТАРОлогический прогноз", callback_data="tarot")],
            [Btn("📑 Как это работает?", callback_data="how")],
            [Btn(f"💳 Пополнить баланс ({credits})", callback_data="pay")],
            [Btn("👥 Пригласи друга", callback_data="ref")],
            [Btn("🛟 Техподдержка", url=f"https://t.me/{SUPPORT.lstrip('@')}")],
        ]
    )


def kb_back():
    return Mk([[Btn("← Назад", callback_data="back")]])


def kb_pay():
    return Mk(
        [
            [
                Btn("2 анализа – 100 ₽", callback_data="buy_2_card"),
                Btn("2 анализа – 55 ⭐", callback_data="buy_2_star"),
            ],
            [
                Btn("5 анализов – 200 ₽", callback_data="buy_5_card"),
                Btn("5 анализов – 110 ⭐", callback_data="buy_5_star"),
            ],
            [
                Btn("10 анализов – 389 ₽", callback_data="buy_10_card"),
                Btn("10 анализов – 220 ⭐", callback_data="buy_10_star"),
            ],
            [Btn("← Назад", callback_data="back")],
        ]
    )


def kb_topup(credits: int = 0):
    return Mk([[Btn(f"💳 Пополнить баланс ({credits})", callback_data="pay")]])


def kb_scenario():
    return Mk(
        [
            [Btn("🧩 Подход к коллеге", callback_data="scn_colleague")],
            [Btn("💘 Ключ к сердцу", callback_data="scn_romance")],
            [Btn("🧠 Клиент перед продажей", callback_data="scn_sales")],
            [Btn("🧬 Общий анализ", callback_data="scn_common")],
            [Btn("← Назад", callback_data="back")],
        ]
    )


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
) -> Message:
    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except BadRequest:
            pass
        ctx.user_data.pop("menu_id", None)
        old = None
    else:
        old = ctx.user_data.pop("menu_id", None)

    m = await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=pm)

    if menu:
        if old and old != m.message_id:
            try:
                await ctx.bot.delete_message(m.chat_id, old)
            except BadRequest:
                pass
        ctx.user_data["menu_id"] = m.message_id

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
    uid = update.effective_user.id
    args = update.message.text.split(maxsplit=1)[1:] if update.message else []

    first_visit = is_new_user(uid)

    if args:
        ref = args[0]
        parent = int(ref) if ref.isdigit() else alias_owner(ref)
        if parent and set_referral(uid, parent):
            add_credits(parent, BONUS_REF)
            try:
                name = html.escape(update.effective_user.full_name)
                link = f'<a href="tg://user?id={uid}">{name}</a>'
                total = get_credits(parent, ADMIN_IDS)
                await ctx.bot.send_message(
                    parent,
                    f"🎉 {link} (ID {uid}) присоединился по вашей ссылке!\n"
                    f"+{BONUS_REF} анализа начислено. Теперь доступно: {total}.",
                    parse_mode=ParseMode.HTML,
                )
            except NetworkError:
                pass

    log_event("start", uid)

    # 1-й визит → автоматически +1
    if first_visit:
        add_credits(uid, 1)

    await show_main(update, ctx, uid)


# ── Router ──────────────────────────────────────────
async def router(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    uid, data = update.effective_user.id, q.data
    log_event(data, uid)
    credits = get_credits(uid, ADMIN_IDS)

    match data:
        case "scan":
            if credits <= 0:
                await show(
                    "❌ Проверок нет. Пополни баланс.",
                    update,
                    ctx,
                    kb_topup(credits),
                    menu=False,
                )
                return
            await show(
                "Что именно ты хочешь узнать?",
                update,
                ctx,
                kb_scenario(),
            )
        case "tarot":
            if credits <= 0:
                await show(
                    "❌ Проверок нет. Пополни баланс.",
                    update,
                    ctx,
                    kb_topup(credits),
                    menu=False,
                )
                return
            ctx.user_data["scenario"] = "tarot"
            schedule_photo_timeout(ctx, uid)
            await show(
                "📷 Скиньте фото, сделанное сегодня.\n\n"
                "Мы считываем твою мимику, эмоции, состояние — и превращаем их в пять «карт»:\n"
                "архетипы из системы Таро, но без суеверий. Вместо колоды — ты сам.\n"
                "Твое лицо как маршрут на день: что внутри движется, что мешает, что поддерживает, и к чему зовёт твоя энергия прямо сейчас.\n\n"
                "🗝️ Это инструмент для понимания, не для предсказания.\n"
                "И пусть сегодняшний день начнётся с понимания того, кто ты есть именно сейчас.\n\n"
                "—\n"
                "Бот работает, подождите около 40 секунд до ответа.",
                update,
                ctx,
                kb_back(),
            )
        case "how":
            await show(HOW_IT_WORKS, update, ctx, kb_back())
        case "pay":
            await show("💳 *Выбери пакет:*", update, ctx, kb_pay())
        case "ref":
            names = aliases_for(uid)
            code = names[0] if names else str(uid)
            link = f"https://t.me/{ctx.bot.username}?start={code}"
            await show(
                "Если у тебя есть друзья, которым ты тоже желаешь только "
                "успешных знакомств, приглашай их и получай бонусы!\n\n"
                f"🔗 Твоя ссылка: `{link}`\n\n"
                f"За каждого нового пользователя — *+{BONUS_REF} анализа*.",
                update,
                ctx,
                kb_back(),
            )
        case _ if data.startswith("scn_"):
            if credits <= 0:
                await show(
                    "❌ Проверок нет. Пополни баланс.",
                    update,
                    ctx,
                    kb_topup(credits),
                    menu=False,
                )
                return
            scn = data.split("_", 1)[1]
            ctx.user_data["scenario"] = scn
            schedule_photo_timeout(ctx, uid)
            await show(
                "📷 Пришли фото для анализа.\n\n"
                "Лицо крупным планом, без маски или посторонних предметов "
                "(очки допустимы, но лучше без них). "
                "На фото должен быть только один человек.\n\n"
                "—\n"
                "Бот работает, подождите около 40 секунд до ответа.",
                update,
                ctx,
                kb_back(),
            )
        case "back":
            scn = ctx.user_data.pop("scenario", None)
            cancel_photo_timeout(ctx, uid)
            if scn:
                log_event(f"cancel_{scn}", uid)
            await show_main(update, ctx, uid)
        case _ if data.startswith("buy_"):
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
                title=f"{qty} анализа",
                description="Пакет анализов",
                payload=f"buy_{qty}_{method}",
                provider_token=token,
                currency=currency,
                prices=[LabeledPrice(f"{qty} анализа", price)],
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
        case "back_pay":
            try:
                await q.message.delete()
            except BadRequest:
                pass
            await show("💳 *Выбери пакет:*", update, ctx, kb_pay())


# ── Payment ─────────────────────────────────────────
async def pre_checkout(update: Update, ctx):
    await update.pre_checkout_query.answer(True)


async def paid(update: Update, ctx):
    parts = update.message.successful_payment.invoice_payload.split("_")
    qty = int(parts[1])
    method = parts[2] if len(parts) > 2 else "card"
    uid = update.effective_user.id
    log_event("paid", uid)
    log_payment(uid, qty, method)
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
        f"✅ Оплата прошла!\n\nДоступно: *{credits}* анализов.\n",
        update,
        ctx,
        kb_main(uid, credits),
        menu=False,
        ttl=5,
    )
    await show_main(update, ctx, uid)


# ── Фото ────────────────────────────────────────────
async def handle_photo(update: Update, ctx):
    uid = update.effective_user.id
    log_event("photo", uid)
    cancel_photo_timeout(ctx, uid)
    if (credits := get_credits(uid, ADMIN_IDS)) <= 0:
        await update.message.reply_text(
            "❌ Проверок нет. Пополни баланс.",
            reply_markup=kb_topup(credits),
        )
        return

    scenario = ctx.user_data.get("scenario", "common")

    async with SEM, TypingCtx(ctx.bot, update.message.chat_id):
        try:
            raw = await (
                await ctx.bot.get_file(update.message.photo[-1].file_id)
            ).download_as_bytearray()

            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((640, 640), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)

            loop = asyncio.get_running_loop()
            attrs = await loop.run_in_executor(None, detect_attributes, buf.getvalue())
            report = await loop.run_in_executor(None, build_profile, attrs, scenario)
        except Exception as e:
            log.exception("photo processing error", exc_info=e)
            ctx.user_data["scenario"] = scenario
            await update.message.reply_text(
                "⛔ Фотография неподходящая для анализа. Попробуйте прислать изображение, где лицо видно лучше, без масок и посторонних предметов.",
                reply_markup=kb_back(),
            )
            return

    ctx.user_data.pop("scenario", None)
    log_event(f"ok_{scenario}", uid)

    report = re.sub(r"^\s*#+\s*", "", report, flags=re.MULTILINE)
    footer = FOOTER_TAROT if scenario == "tarot" else FOOTER
    await update.message.reply_text(report + footer, parse_mode="Markdown")
    dec_credit(uid, ADMIN_IDS)
    credits = get_credits(uid, ADMIN_IDS)
    await show(
        f"Доступно: *{credits}* анализов.",
        update,
        ctx,
        kb_main(uid, credits),
        menu=False,
        ttl=5,
    )
    if credits == 0:
        await update.effective_chat.send_message(
            "⛔ Проверок больше нет. Нажми «💳 Пополнить баланс».",
            reply_markup=kb_topup(credits),
        )
    await show_main(update, ctx, uid)
    schedule_reminders(ctx, uid)


async def reminder1(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.chat_id
    credits = get_credits(uid, ADMIN_IDS)
    if credits > 0:
        try:
            await ctx.bot.send_message(
                uid, REMINDER_TXT1, reply_markup=kb_main(uid, credits)
            )
        except BadRequest:
            pass

async def reminder2(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.chat_id
    credits = get_credits(uid, ADMIN_IDS)
    if credits > 0:
        try:
            await ctx.bot.send_message(
                uid, REMINDER_TXT2, reply_markup=kb_main(uid, credits)
        )
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


def cancel_photo_timeout(ctx, uid):
    for job in ctx.job_queue.get_jobs_by_name(f"photo_timeout_{uid}"):
        job.schedule_removal()


async def photo_timeout(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.chat_id
    scn = ctx.application.user_data[uid].pop("scenario", None)
    if scn:
        try:
            credits = get_credits(uid, ADMIN_IDS)
            await ctx.bot.send_message(
                uid,
                "⌛ Время ожидания фото истекло.",
                reply_markup=kb_main(uid, credits),
            )
        except BadRequest:
            pass


def schedule_photo_timeout(ctx, uid):
    cancel_photo_timeout(ctx, uid)
    ctx.job_queue.run_once(
        photo_timeout, PHOTO_TIMEOUT_SEC, chat_id=uid, name=f"photo_timeout_{uid}"
    )


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
                f"🎁 Вам начислено {qty} анализов от администратора.\nДоступно: {total}.",
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
        except BadRequest:
            pass
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
        await update.message.reply_text(f"🔗 Твоя ссылка: {link}")
    else:
        await update.message.reply_text("❌ Имя занято.")


async def stats(update: Update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    day1 = count_new_users(1)
    day3 = count_new_users(3)
    week = count_new_users(7)
    month = count_new_users(30)
    events = event_stats()
    scans = {scn: events.get(f"ok_{scn}", 0) for scn in ("colleague", "romance", "sales", "common", "tarot")}
    total = sum(scans.values())
    pay_visits = unique_event_users("pay")
    pay_clicks = unique_event_users_like("buy_")
    pay_backs = unique_event_users("back_pay")

    rows = [
        ("Новых за 1д", day1),
        ("за 3д", day3),
        ("за 7д", week),
        ("за 30д", month),
        ("Перешли к оплате", pay_visits),
        ("Нажали оплатить", pay_clicks),
        ("Назад из оплаты", pay_backs),
        ("Всего проверок", total),
        ("Коллега", scans["colleague"]),
        ("Отношения", scans["romance"]),
        ("Продажи", scans["sales"]),
        ("Общий", scans["common"]),
        ("Таро", scans["tarot"]),
    ]

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
            rub[uid] = rub.get(uid, 0) + PACKS_RUB[qty] * cnt
        else:
            star[uid] = star.get(uid, 0) + PACKS_STARS[qty] * cnt

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
    req = HTTPXRequest(connect_timeout=10, read_timeout=20)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .job_queue(JobQueue())
        .build()
    )

    app.add_handlers(
        [
            CommandHandler("start", start),
            CommandHandler("grant", grant),
            CommandHandler("broadcast", broadcast),
            CommandHandler("alias", set_alias_cmd),
            CommandHandler("stats", stats),
            CommandHandler("stats_referrals", stats_referrals),
            CallbackQueryHandler(router),
            PreCheckoutQueryHandler(pre_checkout),
            MessageHandler(filters.SUCCESSFUL_PAYMENT, paid),
            MessageHandler(filters.PHOTO, handle_photo),
            ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        ]
    )
    app.add_error_handler(err)

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
