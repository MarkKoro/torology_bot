import os, json, datetime as dt
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

NUM_PROMPT = (
    "Ты профессиональный нумеролог. На основе даты рождения сделай короткий анализ. "
    "Учитывай пол и выбранную область."
)

TAROT_PROMPT = (
    "Ты опытный таролог. Дай толкование выбранных карт кратко и по делу."
)


def build_numerology(dob1: str, gender: str | None = None, topic: str | None = None, dob2: str | None = None) -> str:
    data = {
        "dob": dob1,
        "gender": gender,
        "topic": topic,
        "dob2": dob2,
    }
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": NUM_PROMPT},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        max_tokens=800,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


def build_tarot(cards: list[dict], spread: int, dob: str | None = None, gender: str | None = None) -> str:
    data = {
        "spread": spread,
        "cards": cards,
        "dob": dob,
        "gender": gender,
        "today": dt.date.today().isoformat(),
    }
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": TAROT_PROMPT},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        max_tokens=800,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()
