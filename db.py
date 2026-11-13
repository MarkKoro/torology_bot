# db.py
"""
Mini-ORM для работы с балансом и рефералами.
Файл `credits.db` создаётся автоматически рядом с ботом.

▪️  users(id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0,
          parent INTEGER, joined TIMESTAMP)
        – parent  → id того, кто пригласил (одноуровневая рефералка)

Все запросы подготовлены, соединение одно (WAL), поэтому
SQLite выдерживает сотни RPS даже на дешёвом VPS.
"""

import sqlite3, threading, time
from pathlib import Path
from typing import Optional, Iterable

DB_PATH = Path(__file__).with_name("credits.db")
_LOCK = threading.RLock()


# ── init ─────────────────────────────────────────────
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH, check_same_thread=False, isolation_level=None
    )  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users(
                        id      INTEGER PRIMARY KEY,
                        credits INTEGER DEFAULT 0,
                        parent  INTEGER,
                        joined  INTEGER,
                        menu_msg INTEGER DEFAULT NULL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_parent ON users(parent)")
    try:
        conn.execute("ALTER TABLE users ADD COLUMN menu_msg INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events(
                        event   TEXT,
                        uid     INTEGER,
                        ts      INTEGER)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_event_ts ON events(event, ts)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS aliases(
                        alias TEXT PRIMARY KEY,
                        uid   INTEGER)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_alias_uid ON aliases(uid)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payments(
                        provider_charge_id TEXT PRIMARY KEY,
                        uid     INTEGER,
                        qty     INTEGER,
                        method  TEXT,
                        ts      INTEGER)"""
    )
    try:
        conn.execute(
            "ALTER TABLE payments ADD COLUMN provider_charge_id TEXT"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_charge ON payments(provider_charge_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_payments_uid ON payments(uid)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ab_events(
                        uid     INTEGER,
                        ts      INTEGER,
                        kind    TEXT,
                        variant TEXT,
                        length  INTEGER)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ab_events_variant ON ab_events(variant)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_cards(
                        uid      INTEGER,
                        day      TEXT,
                        card_id  TEXT,
                        reversed INTEGER,
                        text     TEXT,
                        ts       INTEGER,
                        PRIMARY KEY(uid, day))"""
    )
    _migrate(conn)
    return conn


def _add_column(conn, table: str, ddl: str) -> bool:
    """ALTER TABLE ... ADD COLUMN; True если колонка добавлена сейчас."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError:
        return False


def _migrate(conn):
    """Только добавляющие миграции — старые данные не трогаем."""
    _add_column(conn, "users", "dob TEXT")
    _add_column(conn, "users", "gender TEXT")
    _add_column(conn, "users", "screen_kind TEXT")
    if _add_column(conn, "users", "ref_rewarded INTEGER DEFAULT 0"):
        # Всем существующим рефералам бонус уже был начислен старой логикой.
        conn.execute("UPDATE users SET ref_rewarded=1")
    _add_column(conn, "payments", "amount INTEGER")
    _add_column(conn, "payments", "currency TEXT")


DB = _get_conn()


def init_sub():
    """Добавляет колонки кэширования подписки, если их ещё нет."""
    with _LOCK:
        try:
            DB.execute(
                "ALTER TABLE users ADD COLUMN subscribed INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:
            DB.execute(
                "ALTER TABLE users ADD COLUMN last_sub_check INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass


# ── util ─────────────────────────────────────────────
def _ensure(uid: int) -> bool:
    """Добавить пользователя при первом обращении.

    Возвращает ``True``, если запись была создана прямо сейчас."""
    cur = DB.execute(
        "INSERT OR IGNORE INTO users(id,joined) VALUES(?,?)",
        (uid, int(time.time())),
    )
    return cur.rowcount > 0


def is_new_user(uid: int) -> bool:
    "Возвращает True, если пользователь ранее не создавался."
    with _LOCK:
        cur = DB.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone()
        return cur is None


def register_user(uid: int) -> bool:
    "Создаёт пользователя. True — если он новый."
    with _LOCK:
        return _ensure(uid)


# ── API ──────────────────────────────────────────────
def get_credits(uid: int, admins: Iterable[int] = ()) -> int:
    """
    • админам всегда ∞ (10 000)
    • новый пользователь → 0
    """
    if uid in admins:
        return 10_000
    with _LOCK:
        _ensure(uid)
        row = DB.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
        return row[0] if row else 0


def add_credits(uid: int, qty: int):
    if qty <= 0:
        return
    with _LOCK:
        _ensure(uid)
        DB.execute("UPDATE users SET credits = credits + ? WHERE id=?", (qty, uid))


def dec_credit(uid: int, admins: Iterable[int] = (), qty: int = 1):
    "Списать `qty` кредитов (если не админ); < 0 не уходим"
    if uid in admins:
        return
    if qty <= 0:
        return
    with _LOCK:
        DB.execute(
            "UPDATE users SET credits = MAX(credits - ?, 0) WHERE id=?",
            (qty, uid),
        )


def all_user_ids() -> list[int]:
    "Возвращает список всех id пользователей."
    with _LOCK:
        rows = DB.execute("SELECT id FROM users").fetchall()
    return [r[0] for r in rows]


def get_menu_msg(uid: int) -> Optional[int]:
    with _LOCK:
        row = DB.execute("SELECT menu_msg FROM users WHERE id=?", (uid,)).fetchone()
    return row[0] if row and row[0] else None


def set_menu_msg(uid: int, msg_id: int):
    with _LOCK:
        _ensure(uid)
        DB.execute("UPDATE users SET menu_msg=? WHERE id=?", (msg_id, uid))


# ── события и статистика ─────────────────────────────
def log_event(event: str, uid: int):
    with _LOCK:
        DB.execute(
            "INSERT INTO events(event, uid, ts) VALUES(?,?,?)",
            (event, uid, int(time.time())),
        )


def count_new_users(days: int) -> int:
    border = int(time.time()) - days * 86400
    with _LOCK:
        row = DB.execute(
            "SELECT COUNT(*) FROM users WHERE joined>=?", (border,)
        ).fetchone()
    return row[0] if row else 0


def get_subscribed(uid: int) -> tuple[int, int]:
    """Возвращает (subscribed, last_sub_check) для пользователя."""
    with _LOCK:
        row = DB.execute(
            "SELECT subscribed, last_sub_check FROM users WHERE id=?",
            (uid,),
        ).fetchone()
    return (row[0] if row else 0, row[1] if row else 0)


def set_subscribed(uid: int, ok: bool, now_ts: int):
    """Сохраняет результат проверки подписки."""
    with _LOCK:
        _ensure(uid)
        DB.execute(
            "UPDATE users SET subscribed=?, last_sub_check=? WHERE id=?",
            (1 if ok else 0, now_ts, uid),
        )


def event_stats() -> dict[str, int]:
    with _LOCK:
        rows = DB.execute(
            "SELECT event, COUNT(*) FROM events GROUP BY event"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def unique_event_users(event: str) -> int:
    with _LOCK:
        row = DB.execute(
            "SELECT COUNT(DISTINCT uid) FROM events WHERE event=?",
            (event,)
        ).fetchone()
    return row[0] if row else 0


def unique_event_users_like(prefix: str) -> int:
    """Количество уникальных пользователей для событий, начинающихся с prefix."""
    with _LOCK:
        row = DB.execute(
            "SELECT COUNT(DISTINCT uid) FROM events WHERE event LIKE ?",
            (prefix + "%",),
        ).fetchone()
    return row[0] if row else 0


def event_count(uid: int, event: str) -> int:
    """Возвращает количество событий ``event`` для пользователя ``uid``."""
    with _LOCK:
        row = DB.execute(
            "SELECT COUNT(*) FROM events WHERE uid=? AND event=?",
            (uid, event),
        ).fetchone()
    return row[0] if row else 0


# ── рефералка ───────────────────────────────────────
def set_referral(child: int, parent: int) -> bool:
    """
    Возвращает True, если связь установлена впервые.
    Игнорируется, когда:
        • child == parent
        • child уже имеет parent
    """
    if child == parent:  # нельзя пригласить себя
        return False
    with _LOCK:
        _ensure(child)
        _ensure(parent)
        cur = DB.execute("SELECT parent FROM users WHERE id=?", (child,)).fetchone()
        if cur and cur[0]:  # уже есть родитель
            return False
        DB.execute("UPDATE users SET parent=? WHERE id=?", (parent, child))
        return True


def referral_counts() -> list[tuple[int, int]]:
    """Возвращает список (uid, количество приглашённых)."""
    with _LOCK:
        rows = DB.execute(
            "SELECT parent, COUNT(*) FROM users WHERE parent IS NOT NULL GROUP BY parent"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def referral_count(uid: int) -> int:
    with _LOCK:
        row = DB.execute(
            "SELECT COUNT(*) FROM users WHERE parent=?",
            (uid,),
        ).fetchone()
    return row[0] if row else 0


def add_alias(uid: int, alias: str) -> bool:
    """Создаёт текстовый реферальный код для пользователя."""
    with _LOCK:
        try:
            DB.execute("INSERT INTO aliases(alias, uid) VALUES(?,?)", (alias, uid))
            return True
        except sqlite3.IntegrityError:
            return False


def alias_owner(alias: str) -> Optional[int]:
    """Возвращает id владельца реферального кода или None."""
    with _LOCK:
        row = DB.execute("SELECT uid FROM aliases WHERE alias=?", (alias,)).fetchone()
    return row[0] if row else None


def aliases_for(uid: int) -> list[str]:
    """Возвращает список реферальных имён пользователя."""
    with _LOCK:
        rows = DB.execute("SELECT alias FROM aliases WHERE uid=?", (uid,)).fetchall()
    return [r[0] for r in rows]


def log_payment(
    uid: int,
    qty: int,
    method: str,
    charge_id: str,
    amount: int | None = None,
    currency: str | None = None,
) -> bool:
    """Сохраняет факт оплаты. Возвращает True, если запись новая.

    amount — в минимальных единицах (копейки для RUB, звёзды для XTR)."""
    with _LOCK:
        cur = DB.execute(
            "INSERT OR IGNORE INTO payments(provider_charge_id, uid, qty, method, ts, amount, currency)"
            " VALUES(?,?,?,?,?,?,?)",
            (charge_id, uid, qty, method, int(time.time()), amount, currency),
        )
        return cur.rowcount > 0


def payment_rows(since_days: int | None = None) -> list[tuple[str, int, int | None, str | None, int]]:
    """(method, qty, amount, currency, uid) — для подсчёта выручки."""
    sql = "SELECT method, qty, amount, currency, uid FROM payments"
    args: tuple = ()
    if since_days is not None:
        sql += " WHERE ts>=?"
        args = (int(time.time()) - since_days * 86400,)
    with _LOCK:
        return DB.execute(sql, args).fetchall()


def referral_payment_rows(parent: int | None = None) -> list[tuple[int, str, int, int | None]]:
    """(parent, method, qty, amount) по оплатам приглашённых."""
    sql = (
        "SELECT u.parent, p.method, p.qty, p.amount FROM payments p "
        "JOIN users u ON u.id=p.uid WHERE u.parent IS NOT NULL"
    )
    args: tuple = ()
    if parent is not None:
        sql += " AND u.parent=?"
        args = (parent,)
    with _LOCK:
        return DB.execute(sql, args).fetchall()


# ── экран (одно активное сообщение) ─────────────────
def get_screen(uid: int) -> tuple[Optional[int], Optional[str]]:
    with _LOCK:
        row = DB.execute("SELECT menu_msg, screen_kind FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return None, None
    return (row[0] or None), row[1]


def set_screen(uid: int, msg_id: Optional[int], kind: Optional[str]):
    with _LOCK:
        _ensure(uid)
        DB.execute("UPDATE users SET menu_msg=?, screen_kind=? WHERE id=?", (msg_id, kind, uid))


# ── баланс: атомарное списание ──────────────────────
def spend_credits(uid: int, qty: int, admins: Iterable[int] = ()) -> bool:
    """Списывает qty баллов, только если их хватает. True — списано."""
    if uid in admins or qty <= 0:
        return True
    with _LOCK:
        _ensure(uid)
        cur = DB.execute(
            "UPDATE users SET credits = credits - ? WHERE id=? AND credits >= ?",
            (qty, uid, qty),
        )
        return cur.rowcount > 0


def refund_credits(uid: int, qty: int, admins: Iterable[int] = ()):
    if uid in admins:
        return
    add_credits(uid, qty)


# ── профиль ─────────────────────────────────────────
def get_profile(uid: int) -> tuple[Optional[str], Optional[str]]:
    with _LOCK:
        row = DB.execute("SELECT dob, gender FROM users WHERE id=?", (uid,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def set_profile(uid: int, dob: Optional[str] = None, gender: Optional[str] = None):
    with _LOCK:
        _ensure(uid)
        if dob is not None:
            DB.execute("UPDATE users SET dob=? WHERE id=?", (dob, uid))
        if gender is not None:
            DB.execute("UPDATE users SET gender=? WHERE id=?", (gender, uid))


# ── реферальная награда ─────────────────────────────
def claim_ref_reward(child: int) -> Optional[int]:
    """Возвращает id пригласившего, если награду за child ещё не выдавали."""
    with _LOCK:
        row = DB.execute(
            "SELECT parent, ref_rewarded FROM users WHERE id=?", (child,)
        ).fetchone()
        if not row or not row[0] or row[1]:
            return None
        DB.execute("UPDATE users SET ref_rewarded=1 WHERE id=?", (child,))
        return row[0]


# ── карта дня ───────────────────────────────────────
def get_daily(uid: int, day: str) -> Optional[tuple[str, bool, str]]:
    with _LOCK:
        row = DB.execute(
            "SELECT card_id, reversed, text FROM daily_cards WHERE uid=? AND day=?",
            (uid, day),
        ).fetchone()
    return (row[0], bool(row[1]), row[2]) if row else None


def save_daily(uid: int, day: str, card_id: str, reversed_: bool, text: str):
    with _LOCK:
        DB.execute(
            "INSERT OR REPLACE INTO daily_cards(uid, day, card_id, reversed, text, ts) VALUES(?,?,?,?,?,?)",
            (uid, day, card_id, 1 if reversed_ else 0, text, int(time.time())),
        )


def daily_stats(days: int = 1) -> tuple[int, int]:
    border = int(time.time()) - days * 86400
    with _LOCK:
        row = DB.execute(
            "SELECT COUNT(*), COUNT(DISTINCT uid) FROM daily_cards WHERE ts>=?", (border,)
        ).fetchone()
    return (row[0], row[1]) if row else (0, 0)


def log_ab_event(uid: int, kind: str, variant: str, length: int):
    with _LOCK:
        DB.execute(
            "INSERT INTO ab_events(uid, ts, kind, variant, length) VALUES(?,?,?,?,?)",
            (uid, int(time.time()), kind, variant, length),
        )


def referral_payment_stats() -> list[tuple[int, str, int, int]]:
    """Статистика оплат по родителю (uid, метод, пакет, количество покупок)."""
    with _LOCK:
        rows = DB.execute(
            """SELECT u.parent, p.method, p.qty, COUNT(*)
                     FROM payments p
                     JOIN users u ON u.id=p.uid
                     WHERE u.parent IS NOT NULL
                     GROUP BY u.parent, p.method, p.qty"""
        ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def referral_payment_stats_user(uid: int) -> list[tuple[str, int, int]]:
    """Статистика оплат по конкретному пригласителю (метод, пакет, количество)."""
    with _LOCK:
        rows = DB.execute(
            """SELECT p.method, p.qty, COUNT(*)
                     FROM payments p
                     JOIN users u ON u.id=p.uid
                     WHERE u.parent=?
                     GROUP BY p.method, p.qty""",
            (uid,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def payment_stats() -> list[tuple[str, int, int]]:
    """Общая статистика оплат (метод, пакет, количество)."""
    with _LOCK:
        rows = DB.execute(
            "SELECT method, qty, COUNT(*) FROM payments GROUP BY method, qty"
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def payment_stats_since(days: int) -> list[tuple[str, int, int]]:
    """Статистика оплат за последние ``days`` дней."""
    border = int(time.time()) - days * 86400
    with _LOCK:
        rows = DB.execute(
            "SELECT method, qty, COUNT(*) FROM payments WHERE ts>=? GROUP BY method, qty",
            (border,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def unique_payers() -> int:
    """Количество пользователей, совершивших хоть одну оплату."""
    with _LOCK:
        row = DB.execute("SELECT COUNT(DISTINCT uid) FROM payments").fetchone()
    return row[0] if row else 0

