"""Тексты толкований через DeepSeek (OpenAI-совместимый API).

Модель получает уже готовые данные: карты с позициями и перевёрнутостью,
посчитанные нумерологические числа, тему и вопрос. Её задача — толковать.
"""
import datetime as dt
import json
import os
import re
import threading

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
    timeout=90,
    max_retries=1,
)
# deepseek-chat/deepseek-reasoner — устаревшие имена; актуальная дешёвая модель —
# deepseek-flash (DeepSeek-V4.1-Flash). Режим размышлений выключаем: для толкований
# он не нужен, а «думающие» токены оплачиваются и съедают лимит max_tokens.
MODEL = os.getenv("LLM_MODEL", "deepseek-flash")
EXTRA_BODY = {"thinking": {"type": "disabled"}}

_LLM_LOCK = threading.Lock()
_LLM_COUNT = 0


def llm_queue_size() -> int:
    with _LLM_LOCK:
        return _LLM_COUNT


STYLE = """
СТИЛЬ:
— Только русский язык, обращение на «ты», тёплый спокойный тон взрослого человека.
— Если указан пол, согласуй с ним окончания. К самому человеку обращайся только на «ты»; о паре говори «вы с ним» / «вы с ней» или «вы оба», без ошибок в роде.
— Никакого markdown: не используй символы # * _ ~ ` > и не делай списков с тире или цифрами.
— Короткие абзацы по 2–4 предложения, между абзацами пустая строка.
— Без штампов, пафоса, мистического тумана и запугивания. Не пророчь «навсегда», не называй точных дат и процентов.
— Не объясняй методику и не пиши о себе как о модели. Сразу текст для человека.
— Пиши конкретно: что происходит, что из этого следует и что сделать на практике."""

TAROT_PROMPT = """Ты — опытный таролог. Человек сделал расклад и ждёт толкования, которое поможет ему разобраться и решить, что делать.

Во входных данных: формат расклада, карты по позициям (с пометкой перевёрнутости), тема и, возможно, вопрос человека, пол (может отсутствовать) и сегодняшняя дата.

КАК ТОЛКОВАТЬ:
— Если есть вопрос — весь текст отвечает на него. Если вопроса нет — держись темы.
— Толкуй каждую карту именно в её позиции и называй карту по имени, чтобы человек видел связь с раскладом. Затем свяжи карты в единую историю.
— Перевёрнутая карта — это задержка, внутренний блок или перекос энергии карты, а не катастрофа.
— Заверши абзацем «что делать»: 2–3 конкретных шага на ближайшее время и одну вещь, которой лучше избегать.
— Первая фраза — сразу о сути, без приветствий.
{style}

ОБЪЁМ: {size}."""

TAROT_SIZE = {
    1: "900–1300 символов",
    3: "1800–2400 символов",
    5: "2400–3000 символов, не больше 3200",
}

DAILY_PROMPT = """Ты — таролог. Человек открыл свою карту дня. Напиши короткое послание на сегодня.

Построй текст так: одно предложение о главной энергии дня через эту карту (назови карту), затем что сегодня получится особенно хорошо, на что быть внимательнее и один маленький практический совет на день. Перевёрнутая карта — повод притормозить и скорректировать курс, а не плохой знак.
{style}

ОБЪЁМ: строго 450–650 символов. Это подпись к картинке, длиннее нельзя."""

NUM_PROMPT = """Ты — нумеролог-практик пифагорейской школы. Числа уже посчитаны (они во входных данных) — не пересчитывай их и не объясняй формулы. Твоя задача — толкование.

Во входных данных: дата рождения, посчитанные числа (число судьбы, число дня рождения, личный год и личный месяц), тема запроса, пол (может отсутствовать), дата рождения второго человека и его число судьбы (если есть), сегодняшняя дата.

ЗАДАЧА:
— Начни с сути характера через число судьбы, назови само число один раз.
— Покажи, где эти качества помогают, а где мешают — именно в выбранной теме.
— Объясни, что значит текущий личный год и личный месяц и на что направить силы сейчас.
— Если есть второй человек — разбери, как ваши числа сочетаются, где вы дополняете друг друга, где возможны трения и как их сглаживать.
— Заверши практичными рекомендациями на ближайшие месяцы.
{topic}
{style}

ОБЪЁМ: 2400–3000 символов, не больше 3200."""

NUM_TOPIC_PROMPTS = {
    "Отношения": "ТЕМА: личные отношения и совместимость. Не уходи в карьеру.",
    "Семья": "ТЕМА: семья, роли, взаимодействие поколений, атмосфера дома. Не уходи в карьеру.",
    "Саморазвитие": "ТЕМА: личный рост, таланты, внутренний потенциал.",
    "Работа": "ТЕМА: работа, деньги, профессиональный путь, отношения в коллективе.",
    "Общий анализ": "ТЕМА: целостный портрет и ключевые сферы жизни без перекоса в одну.",
}

_MD_RE = re.compile(r"[#*_~`>]")


def _clean(text: str) -> str:
    text = _MD_RE.sub("", text or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _call(system: str, data: dict, max_tokens: int, temperature: float = 0.8) -> str:
    global _LLM_COUNT
    with _LLM_LOCK:
        _LLM_COUNT += 1
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=EXTRA_BODY,
        )
    finally:
        with _LLM_LOCK:
            _LLM_COUNT -= 1
    text = _clean(resp.choices[0].message.content)
    if len(text) < 200:
        raise RuntimeError("LLM returned too short answer")
    return text


def build_tarot(
    cards: list[dict],
    spread: int,
    topic: str | None = None,
    question: str | None = None,
    gender: str | None = None,
) -> str:
    """cards: [{"position": "Прошлое", "card": "Шут", "reversed": False}, ...]"""
    data = {
        "format": {1: "Совет одной карты", 3: "Три карты", 5: "Большой расклад из пяти карт"}[spread],
        "cards": cards,
        "topic": topic,
        "question": question,
        "gender": gender,
        "today": dt.date.today().isoformat(),
    }
    system = TAROT_PROMPT.format(style=STYLE, size=TAROT_SIZE[spread])
    return _call(system, data, max_tokens={1: 900, 3: 1500, 5: 1900}[spread])


def build_daily(card: str, reversed_: bool, gender: str | None = None) -> str:
    data = {"card": card, "reversed": reversed_, "gender": gender, "today": dt.date.today().isoformat()}
    text = _call(DAILY_PROMPT.format(style=STYLE), data, max_tokens=450)
    return text[:900]


def build_numerology(
    dob1: str,
    numbers: dict,
    topic: str | None = None,
    gender: str | None = None,
    dob2: str | None = None,
    pair: dict | None = None,
) -> str:
    data = {
        "dob": dob1,
        "numbers": numbers,
        "topic": topic,
        "gender": gender,
        "partner_dob": dob2,
        "pair": pair,
        "today": dt.date.today().isoformat(),
    }
    system = NUM_PROMPT.format(topic=NUM_TOPIC_PROMPTS.get(topic or "", ""), style=STYLE)
    return _call(system, data, max_tokens=1900, temperature=0.75)
