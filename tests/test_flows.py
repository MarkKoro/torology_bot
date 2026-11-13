"""Офлайн-симуляция пользовательских сценариев.

Запуск (из корня репозитория):  python3 tests/test_flows.py
Тест копирует проект во временную папку, чтобы не трогать боевую credits.db.
Главный инвариант: в чате у пользователя не больше одного сообщения с кнопками.
"""
import asyncio
import itertools
import os
import shutil
import sys
import tempfile
import types

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp()
for item in ("bot.py", "db.py", "utils", "taro_cards", "Card_back.png"):
    p = os.path.join(SRC, item)
    (shutil.copytree if os.path.isdir(p) else shutil.copy)(p, os.path.join(WORK, item))
sys.path[:0] = [os.path.join(SRC, "tests", "stubs"), WORK]
os.environ.update(ADMINS="999", YK_TOKEN="yk-test", CHANNEL_ID="-100")

import openai  # noqa: E402  (заглушка)
from telegram.error import BadRequest  # noqa: E402

import bot  # noqa: E402
import db  # noqa: E402

_ids = itertools.count(1)


class Msg(types.SimpleNamespace):
    pass


class FakeBot:
    username = "taro_test_bot"

    def __init__(self):
        self.chats: dict[int, dict[int, Msg]] = {}
        self.subscribed: set[int] = set()

    def _add(self, chat_id, **kw):
        mid = next(_ids)
        self.chats.setdefault(chat_id, {})[mid] = Msg(id=mid, deleted=False, **kw)
        return Msg(message_id=mid, chat_id=chat_id)

    def _get(self, chat_id, mid):
        m = self.chats.get(chat_id, {}).get(mid)
        if not m or m.deleted:
            raise BadRequest("Message to edit not found")
        return m

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        assert len(text) <= 4096, len(text)
        return self._add(chat_id, type="text", text=text, kb=reply_markup)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        assert caption is None or len(caption) <= 1024, len(caption)
        return self._add(chat_id, type="photo", text=caption, kb=reply_markup)

    async def send_invoice(self, chat_id, reply_markup=None, **kw):
        return self._add(chat_id, type="invoice", text=kw["title"], kb=reply_markup, inv=kw)

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None, **kw):
        m = self._get(chat_id, message_id)
        if m.type != "text":
            raise BadRequest("There is no text in the message to edit")
        if m.text == text and m.kb is reply_markup:
            raise BadRequest("Message is not modified")
        m.text, m.kb = text, reply_markup

    async def edit_message_media(self, media, chat_id, message_id, reply_markup=None):
        m = self._get(chat_id, message_id)
        if m.type != "photo":
            raise BadRequest("Message can't be edited")
        assert len(media.caption or "") <= 1024
        m.text, m.kb = media.caption, reply_markup

    async def edit_message_caption(self, chat_id, message_id, caption, parse_mode=None, reply_markup=None):
        m = self._get(chat_id, message_id)
        if m.type != "photo":
            raise BadRequest("no caption")
        assert len(caption) <= 1024
        m.text, m.kb = caption, reply_markup

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self._get(chat_id, message_id).kb = reply_markup

    async def delete_message(self, chat_id, message_id):
        self._get(chat_id, message_id).deleted = True

    async def get_chat_member(self, chat_id, uid):
        return types.SimpleNamespace(status="member" if uid in self.subscribed else "left")

    async def send_chat_action(self, *a, **k):
        pass

    # ── хелперы для проверок ──
    def visible(self, chat_id):
        return [m for m in self.chats.get(chat_id, {}).values() if not m.deleted]

    def active(self, chat_id):
        return [m for m in self.visible(chat_id) if m.kb is not None]


class User:
    def __init__(self, fbot, uid, name="Аня"):
        self.bot, self.uid, self.name = fbot, uid, name
        self.ctx = types.SimpleNamespace(bot=fbot, args=[], user_data={})

    def _update(self, **kw):
        base = dict(callback_query=None, message=None)
        base.update(kw)
        return types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=self.uid, first_name=self.name, full_name=self.name),
            effective_chat=types.SimpleNamespace(id=self.uid), **base)

    def _user_msg(self, text, **kw):
        m = self.bot._add(self.uid, type="user", text=text, kb=None)
        fbot, uid = self.bot, self.uid

        async def delete():
            await fbot.delete_message(uid, m.message_id)

        async def reply_text(t, **k):
            return await fbot.send_message(uid, t)

        return types.SimpleNamespace(message_id=m.message_id, text=text, delete=delete,
                                     reply_text=reply_text, **kw)

    def run(self, coro):
        asyncio.get_event_loop().run_until_complete(coro)
        act = self.bot.active(self.uid)
        assert len(act) <= 1, f"больше одного активного сообщения: {[a.text[:40] for a in act]}"

    def start(self, payload=""):
        self.ctx.args = [payload] if payload else []
        self.run(bot.start(self._update(message=self._user_msg("/start")), self.ctx))

    def press(self, data, msg_id=None):
        screen = self.screen()
        mid = msg_id or (screen.id if screen else None)
        fbot, uid = self.bot, self.uid
        answers = []

        async def answer(text=None, show_alert=False):
            answers.append(text)

        async def edit_reply_markup(reply_markup=None):
            await fbot.edit_message_reply_markup(uid, mid, reply_markup)

        q = types.SimpleNamespace(data=data, answer=answer,
                                  message=types.SimpleNamespace(message_id=mid, edit_reply_markup=edit_reply_markup))
        self.run(bot.router(self._update(callback_query=q), self.ctx))
        return answers

    def type(self, text):
        self.run(bot.handle_text(self._update(message=self._user_msg(text)), self.ctx))

    def pay(self, qty, method, charge):
        inv = next(m for m in reversed(self.bot.visible(self.uid)) if m.type == "invoice")
        sp = types.SimpleNamespace(invoice_payload=inv.inv["payload"], total_amount=inv.inv["prices"][0].args[1],
                                   currency=inv.inv["currency"], provider_payment_charge_id="" if method == "star" else charge,
                                   telegram_payment_charge_id=charge)
        self.run(bot.paid(self._update(message=self._user_msg("", successful_payment=sp)), self.ctx))

    def screen(self):
        act = self.bot.active(self.uid)
        return act[0] if act else None

    def buttons(self):
        s = self.screen()
        return s.kb.datas() if s else []

    def credits(self):
        return db.get_credits(self.uid)


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    fb = FakeBot()
    a = User(fb, 101, "Аня")

    # 1. Новый пользователь без подписки → экран подписки, бонус уже начислен
    a.start()
    assert "Почти готово" in a.screen().text and a.credits() == bot.WELCOME_BONUS
    assert a.press("chk_sub")[-1] and "Почти готово" in a.screen().text
    fb.subscribed.add(101)
    a.press("chk_sub")
    assert "Добро пожаловать" in a.screen().text, a.screen().text
    assert len(fb.visible(101)) == 1, "в чате должен остаться только экран"

    # 2. Карта дня: фото-разбор, меню уходит; повтор — без нового запроса к LLM
    a.press("d")
    daily = a.screen()
    assert daily.type == "photo" and "Карта дня" in daily.text
    a.press("m")
    assert daily.kb is None and not daily.deleted, "карта дня остаётся в истории без кнопок"
    a.press("d")
    assert db.event_count(101, "daily") == 1

    # 3. Три карты на тему любви: одна картинка, которая заполняется
    a.press("m")
    a.press("t")
    a.press("ts:3")
    a.press("tt:love")
    pick = a.screen()
    assert pick.type == "photo" and "1 из 3" in pick.text
    a.press("tp:0:0")
    a.press("tp:0:0")  # двойной тап по старому шагу — игнор
    assert a.screen().id == pick.id and "2 из 3" in a.screen().text
    a.press("tp:2:1")
    a.press("tp:5:2")
    assert a.credits() == bot.WELCOME_BONUS - 2, a.credits()
    assert not pick.deleted and pick.kb is None and "Прошлое" in pick.text
    report = a.screen()
    assert report.type == "text" and "Три карты" in report.text and "*" not in report.text
    assert "ts:5" in a.buttons(), "апселл на большой расклад"

    # 4. Не хватает баллов → paywall с пакетами
    a.press("n")
    assert "нужно 3 балла" in a.screen().text and "buy:15:card" in a.buttons()

    # 5. Оплата звёздами дважды (раньше вторая терялась из-за пустого charge id)
    a.press("buy:6:star")
    assert a.screen().type == "invoice"
    a.pay(6, "star", "tg-1")
    assert "Оплата прошла" in a.screen().text
    a.press("buy:6:star")
    a.pay(6, "star", "tg-2")
    assert a.credits() == 1 + 12, a.credits()
    assert not any(m.type == "invoice" for m in fb.visible(101))

    # 5б. Рубли через ЮKassa: общее окно + СБП, начисление по «Проверить» и фоновой сверкой
    from utils import yookassa
    fake = {}

    async def fake_create(body, key):
        pid = "yk-" + key
        fake.setdefault(pid, dict(body, id=pid, status="pending", paid=False,
                                  confirmation={"confirmation_url": "https://yoomoney.ru/c/" + pid}))
        return fake[pid]

    async def fake_fetch(pid):
        return fake[pid]

    yookassa.create, yookassa.fetch = fake_create, fake_fetch
    os.environ.update(YOOKASSA_SHOP_ID="1", YOOKASSA_SECRET_KEY="s")
    bal = a.credits()
    a.press("buy:15:card")
    urls = [b.url for row in a.screen().kb.inline_keyboard for b in row if b.url]
    labels = [b.text for row in a.screen().kb.inline_keyboard for b in row]
    assert len(urls) == 2 and "⚡ Оплатить (через СБП)" in labels and "💳 Оплатить 199 ₽" in labels, labels
    sbp = [v for v in fake.values() if v.get("payment_method_data") == {"type": "sbp"}]
    assert len(sbp) == 1 and len(fake) == 2
    assert all("NumaTaro" in v["description"] and "15 баллов" in v["description"] for v in fake.values())
    assert all(v["metadata"]["project_key"] == "torology" for v in fake.values())
    order_btn = next(d for d in a.buttons() if d and d.startswith("yk:"))
    a.press(order_btn)  # ещё не оплачено
    assert "пока не поступила" in a.screen().text and a.credits() == bal
    a.press("pay")
    a.press("buy:15:card")  # повторное нажатие — те же ссылки, без новых платежей
    assert len(fake) == 2
    sbp[0].update(status="succeeded", paid=True)
    a.press(order_btn)
    assert "Оплата прошла" in a.screen().text and a.credits() == bal + 15, a.screen().text
    a.press(order_btn)  # повторная проверка не начисляет второй раз
    assert a.credits() == bal + 15
    # Вторую ссылку из того же заказа тоже оплатили — фоновая сверка начисляет и её, один раз
    other = next(v for v in fake.values() if v is not sbp[0])
    other.update(status="succeeded", paid=True)
    a.run(bot.yk_reconcile_job(a.ctx))
    a.run(bot.yk_reconcile_job(a.ctx))
    assert a.credits() == bal + 30 and "Оплата прошла" in a.screen().text
    # Подмена суммы в ответе не начисляет баллы
    a.press("buy:6:card")
    bad = [v for v in fake.values() if v["metadata"]["qty"] == "6"]
    for v in bad:
        v.update(status="succeeded", paid=True, amount={"value": "1.00", "currency": "RUB"})
    a.run(bot.yk_reconcile_job(a.ctx))
    assert a.credits() == bal + 30
    del os.environ["YOOKASSA_SHOP_ID"]
    assert db.spend_credits(101, 30)  # дальше сценарии рассчитаны на прежний баланс
    a.press("m")

    # 6. Нумерология: дата → пол → совместимость → ошибка ввода → разбор
    a.press("n")
    assert "Напиши свою дату" in a.screen().text
    a.type("31.02.1990")
    assert "Не получилось прочитать дату" in a.screen().text
    a.type("7.3.1994")
    assert "Уточни пол" in a.screen().text
    a.press("ng:f")
    a.press("nt:rel")
    a.type("21/11/1992")
    rep = a.screen()
    assert "Число судьбы" in rep.text and "Числа пары" in rep.text, rep.text[:300]
    assert a.credits() == 13 - 3
    assert not any(m.type == "user" for m in fb.visible(101)), "ввод пользователя удаляется"
    # повторный заход — дата уже сохранена
    a.press("n")
    assert "07.03.1994" in a.screen().text

    # 7. Ошибка LLM → баллы возвращаются, есть «Повторить»
    a.press("nt:work")
    before = a.credits()
    a.press("m")
    a.press("t")
    a.press("ts:1")
    a.type("Стоит ли менять работу?")
    assert "Стоит ли менять работу" in a.screen().text
    openai.MODE["fail"] = True
    a.press("tp:3:0")
    assert a.credits() == before and "tretry" in a.buttons()
    openai.MODE["fail"] = False
    a.press("tretry")
    assert a.credits() == before - 1 and "Совет карты" in a.screen().text

    # 8. Рефералка: друг пришёл → пригласившему баннер новым экраном
    b = User(fb, 202, "Борис")
    fb.subscribed.add(202)
    bal_a = a.credits()
    b.start("101")
    assert b.credits() == bot.WELCOME_BONUS + bot.REF_FRIEND_BONUS
    assert a.credits() == bal_a + bot.BONUS_REF
    assert "пришёл Борис" in a.screen().text and len(fb.active(101)) == 1
    b.start("101")  # повторный /start не даёт бонусов
    assert a.credits() == bal_a + bot.BONUS_REF

    # 9. Кнопки старой версии бота
    b.press("choose:the_fool:0")
    assert "Меню обновилось" in b.screen().text
    b.press("buy_14_card")
    assert "Меню обновилось" in b.screen().text

    # 10. Нажатие кнопки на старом сообщении — экран переезжает вниз
    prev = a.screen()
    a.press("pay", msg_id=daily.id)
    assert a.screen().id > prev.id and a.screen().id != daily.id, "экран должен переехать вниз"
    assert daily.kb is None and not daily.deleted

    if os.getenv("DUMP"):
        for m in fb.visible(101):
            print(f"--- #{m.id} [{m.type}] buttons={m.kb.datas() if m.kb else None}\n{(m.text or '')[:700]}")

    print("OK: все сценарии прошли;", sum(len(c) for c in fb.chats.values()), "сообщений создано")


if __name__ == "__main__":
    main()
