"""Детерминированные нумерологические расчёты (пифагорейская система).

Модель (LLM) получает уже посчитанные числа и только толкует их —
так результат стабилен и не зависит от «фантазии» нейросети.
"""
from __future__ import annotations

import datetime as dt

MASTER = {11, 22, 33}


def reduce_num(n: int, keep_master: bool = True) -> int:
    """Сводит число к одной цифре, сохраняя мастер-числа 11/22/33."""
    n = abs(int(n))
    while n > 9 and not (keep_master and n in MASTER):
        n = sum(int(c) for c in str(n))
    return n


def _parts(dob: str) -> tuple[int, int, int]:
    d, m, y = (int(x) for x in dob.split("."))
    return d, m, y


def life_path(dob: str) -> int:
    """Число жизненного пути (число судьбы)."""
    d, m, y = _parts(dob)
    return reduce_num(reduce_num(d) + reduce_num(m) + reduce_num(y))


def birthday_number(dob: str) -> int:
    d, _, _ = _parts(dob)
    return reduce_num(d)


def personal_year(dob: str, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    d, m, _ = _parts(dob)
    return reduce_num(reduce_num(d) + reduce_num(m) + reduce_num(today.year))


def personal_month(dob: str, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return reduce_num(personal_year(dob, today) + today.month)


def profile(dob: str, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    return {
        "life_path": life_path(dob),
        "birthday": birthday_number(dob),
        "personal_year": personal_year(dob, today),
        "personal_month": personal_month(dob, today),
        "year": today.year,
    }


def pair(dob1: str, dob2: str) -> dict:
    a, b = life_path(dob1), life_path(dob2)
    return {"life_path_1": a, "life_path_2": b, "pair_number": reduce_num(a + b)}


def header_line(p: dict) -> str:
    return (
        f"Число судьбы: {p['life_path']} · Число дня рождения: {p['birthday']} · "
        f"Личный год {p['year']}: {p['personal_year']}"
    )
