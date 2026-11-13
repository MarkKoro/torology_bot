"""Оплата рублями напрямую через API ЮKassa (как в боте «Решала»).

На каждую покупку создаём два платежа с одинаковой суммой:
  • route="all" — общее окно ЮKassa со всеми способами (карта, SberPay, T-Pay, ЮMoney…);
  • route="sbp" — сразу Система быстрых переводов.
Человек оплачивает любой из них; статус проверяем сами (кнопка «Проверить»
и фоновая сверка), баллы начисляются один раз на каждый оплаченный платёж.
"""
import os
import re
from decimal import Decimal

import httpx

API_URL = "https://api.yookassa.ru/v3/payments"
PROJECT_KEY = "torology"
ROUTES = ("all", "sbp")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def shop() -> tuple[str, str] | None:
    sid = (os.getenv("YOOKASSA_SHOP_ID") or "").strip()
    key = (os.getenv("YOOKASSA_SECRET_KEY") or "").strip()
    return (sid, key) if sid and key else None


def enabled() -> bool:
    return shop() is not None


def payload(uid: int, qty: int, rub: int, route: str, description: str, return_url: str) -> dict:
    if route not in ROUTES:
        raise ValueError("unknown route")
    body = {
        "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
        "capture": True,
        "description": description[:128],
        "metadata": {"project_key": PROJECT_KEY, "user_id": str(uid), "kind": "credits",
                     "qty": str(qty), "route": route},
        "confirmation": {"type": "redirect", "return_url": return_url},
    }
    if route == "sbp":
        body["payment_method_data"] = {"type": "sbp"}
    return body


def validate(payment: dict, uid: int, qty: int, rub: int) -> dict:
    """Проверяем, что ответ ЮKassa — именно наш платёж на ту же сумму и тот же пакет."""
    if not isinstance(payment, dict) or not _ID_RE.match(str(payment.get("id") or "")):
        raise ValueError("bad payment id")
    meta, amount = payment.get("metadata") or {}, payment.get("amount") or {}
    if (meta.get("project_key") != PROJECT_KEY or meta.get("user_id") != str(uid)
            or meta.get("kind") != "credits" or meta.get("qty") != str(qty)
            or amount.get("currency") != "RUB"
            or Decimal(str(amount.get("value"))) != Decimal(rub)):
        raise ValueError("payment mismatch")
    if payment.get("status") not in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
        raise ValueError("unknown status")
    if payment["status"] == "succeeded" and payment.get("paid") is not True:
        raise ValueError("succeeded but not paid")
    return payment


async def create(body: dict, idem_key: str) -> dict:
    sid, key = shop()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(API_URL, json=body, auth=(sid, key), headers={"Idempotence-Key": idem_key})
        r.raise_for_status()
        return r.json()


async def fetch(payment_id: str) -> dict:
    if not _ID_RE.match(payment_id):
        raise ValueError("bad payment id")
    sid, key = shop()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{API_URL}/{payment_id}", auth=(sid, key))
        r.raise_for_status()
        return r.json()
