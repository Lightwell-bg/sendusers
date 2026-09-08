"""broadcast.db — собственное хранилище сервиса рассылок.

Кампании, снапшоты получателей, кэш членства в чатах, постоянный список
заблокировавших бота. Базы ботов этот модуль не трогает (см. sources.py).

SQLite в режиме WAL, одно соединение на процесс. Все роуты в app/main.py —
``async def`` и выполняются прямо в event loop (не в тредпуле), поэтому в
текущей модели запросы к этой БД никогда не идут из разных потоков
одновременно; threading.Lock ниже — подстраховка на случай, если это
изменится (например, появится sync-роут или отдельный поток), а не защита
от актуальной гонки.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .config import settings

# --- статусы кампании ---
CAMPAIGN_STATUSES = (
    "draft", "dry_running", "ready", "scheduled", "running", "paused",
    "done", "failed", "cancelled",
)
# --- статусы получателя ---
RECIPIENT_STATUSES = ("pending", "sending", "sent", "skipped_member", "blocked", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    message_text  TEXT NOT NULL,
    parse_mode    TEXT NOT NULL DEFAULT 'HTML',
    bots          TEXT NOT NULL,                -- 'A' | 'B' | 'A,B'
    image_path    TEXT,                         -- путь к картинке (sendPhoto) или NULL
    status        TEXT NOT NULL DEFAULT 'draft',
    scheduled_at  TEXT,                         -- время запланированной отправки (UTC) или NULL
    created_at    TEXT NOT NULL,
    dry_run_at    TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    totals_json   TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    bot          TEXT NOT NULL,                 -- 'A' | 'B'
    user_id      INTEGER NOT NULL,
    username     TEXT,
    user_name    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    detail       TEXT,
    sent_at      TEXT,
    PRIMARY KEY (campaign_id, bot, user_id)
);
CREATE INDEX IF NOT EXISTS idx_recipients_status
    ON recipients (campaign_id, bot, status);

CREATE TABLE IF NOT EXISTS membership_cache (
    chat        TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,                  -- статус из Telegram или 'unknown'
    checked_at  TEXT NOT NULL,
    PRIMARY KEY (chat, user_id)
);

CREATE TABLE IF NOT EXISTS blocked_users (
    bot         TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    blocked_at  TEXT NOT NULL,
    PRIMARY KEY (bot, user_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now() -> str:
    """Публичная обёртка над _now() — нужна вызывающим за пределами этого
    модуля (worker.py), чтобы сравнивать время с scheduled_at в том же
    формате, не дублируя форматирование."""
    return _now()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(
            settings.broadcast_db_path, check_same_thread=False, timeout=30
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    with _lock:
        get_conn().executescript(_SCHEMA)
        _migrate(get_conn())
        get_conn().commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Мягкие миграции для уже существующей broadcast.db (CREATE TABLE
    IF NOT EXISTS не добавляет колонки в существующую таблицу)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    if "image_path" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN image_path TEXT")
    if "scheduled_at" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN scheduled_at TEXT")


def close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------- campaigns

def create_campaign(title: str, message_text: str, parse_mode: str, bots: str) -> int:
    with _lock:
        cur = get_conn().execute(
            "INSERT INTO campaigns (title, message_text, parse_mode, bots, status, created_at)"
            " VALUES (?, ?, ?, ?, 'draft', ?)",
            (title, message_text, parse_mode, bots, _now()),
        )
        get_conn().commit()
        return cur.lastrowid


def get_campaign(campaign_id: int) -> Optional[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()


def list_campaigns(limit: Optional[int] = None, offset: int = 0) -> list[sqlite3.Row]:
    sql = "SELECT * FROM campaigns ORDER BY id DESC"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [limit, offset]
    return get_conn().execute(sql, params).fetchall()


def count_campaigns() -> int:
    return get_conn().execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]


def transition_campaign(campaign_id: int, from_statuses: Iterable[str], to_status: str) -> bool:
    """Атомарный переход статуса; False, если кампания не в ожидаемом статусе.

    Ключевая защита от двойного запуска: rowcount == 1 только у одного гонщика.
    """
    placeholders = ",".join("?" for _ in from_statuses)
    ts_field = {
        "dry_running": "dry_run_at",
        "running": "started_at",
        "done": "finished_at",
        "failed": "finished_at",
        "cancelled": "finished_at",
    }.get(to_status)
    ts_sql = f", {ts_field} = ?" if ts_field else ""
    params: list[Any] = [to_status] + ([_now()] if ts_field else []) + [campaign_id, *from_statuses]
    with _lock:
        cur = get_conn().execute(
            f"UPDATE campaigns SET status = ?{ts_sql} WHERE id = ? AND status IN ({placeholders})",
            params,
        )
        get_conn().commit()
        return cur.rowcount == 1


def set_campaign_image(campaign_id: int, image_path: Optional[str]) -> None:
    """Привязать/снять картинку кампании (image_path=None — убрать)."""
    with _lock:
        get_conn().execute(
            "UPDATE campaigns SET image_path=? WHERE id=?",
            (image_path, campaign_id),
        )
        get_conn().commit()


def set_campaign_totals(campaign_id: int, totals: dict) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE campaigns SET totals_json = ? WHERE id = ?",
            (json.dumps(totals, ensure_ascii=False), campaign_id),
        )
        get_conn().commit()


def update_campaign_text(campaign_id: int, title: str, message_text: str,
                         parse_mode: str, bots: str) -> bool:
    """Правка кампании возможна только в draft/ready; после правки dry-run
    устаревает, поэтому статус сбрасывается в draft и снапшот чистится."""
    with _lock:
        cur = get_conn().execute(
            "UPDATE campaigns SET title=?, message_text=?, parse_mode=?, bots=?,"
            " status='draft', totals_json=NULL, dry_run_at=NULL"
            " WHERE id=? AND status IN ('draft','ready')",
            (title, message_text, parse_mode, bots, campaign_id),
        )
        if cur.rowcount == 1:
            get_conn().execute("DELETE FROM recipients WHERE campaign_id=?", (campaign_id,))
        get_conn().commit()
        return cur.rowcount == 1


def campaigns_in_status(statuses: Iterable[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in statuses)
    return get_conn().execute(
        f"SELECT * FROM campaigns WHERE status IN ({placeholders}) ORDER BY id",
        list(statuses),
    ).fetchall()


# --------------------------------------------------------------- recipients

def clear_recipients(campaign_id: int) -> None:
    with _lock:
        get_conn().execute("DELETE FROM recipients WHERE campaign_id=?", (campaign_id,))
        get_conn().commit()


def add_recipients(campaign_id: int, bot: str,
                   users: Iterable[tuple[int, Optional[str], Optional[str]]]) -> None:
    """users: (user_id, username, user_name). Дубли молча игнорируются."""
    with _lock:
        get_conn().executemany(
            "INSERT OR IGNORE INTO recipients (campaign_id, bot, user_id, username, user_name)"
            " VALUES (?, ?, ?, ?, ?)",
            [(campaign_id, bot, uid, un, name) for uid, un, name in users],
        )
        get_conn().commit()


def mark_recipient(campaign_id: int, bot: str, user_id: int,
                   status: str, detail: Optional[str] = None) -> None:
    sent_at = _now() if status == "sent" else None
    with _lock:
        get_conn().execute(
            "UPDATE recipients SET status=?, detail=?, sent_at=COALESCE(?, sent_at)"
            " WHERE campaign_id=? AND bot=? AND user_id=?",
            (status, detail, sent_at, campaign_id, bot, user_id),
        )
        get_conn().commit()


def mark_recipients_bulk(campaign_id: int, bot: str, user_ids: Iterable[int],
                         status: str, detail: Optional[str] = None) -> None:
    with _lock:
        get_conn().executemany(
            "UPDATE recipients SET status=?, detail=? WHERE campaign_id=? AND bot=? AND user_id=?",
            [(status, detail, campaign_id, bot, uid) for uid in user_ids],
        )
        get_conn().commit()


def pending_recipients(campaign_id: int, bot: str, limit: int = 200) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM recipients WHERE campaign_id=? AND bot=? AND status='pending'"
        " ORDER BY user_id LIMIT ?",
        (campaign_id, bot, limit),
    ).fetchall()


def claim_next_recipient(campaign_id: int, bot: str) -> Optional[sqlite3.Row]:
    """Двухфазная отправка: атомарно захватывает следующего pending-получателя,
    переводя его в 'sending'. Гарантирует, что даже при случайном втором
    воркере один юзер не будет взят дважды."""
    with _lock:
        row = get_conn().execute(
            "UPDATE recipients SET status='sending' WHERE rowid = ("
            "  SELECT rowid FROM recipients"
            "  WHERE campaign_id=? AND bot=? AND status='pending'"
            "  ORDER BY user_id LIMIT 1)"
            " RETURNING user_id, username, user_name",
            (campaign_id, bot),
        ).fetchone()
        get_conn().commit()
        return row


def reconcile_sending(campaign_id: int) -> int:
    """Восстановление после падения: зависшие 'sending' считаем доставленными
    (at-most-once — для маркетинговой рассылки дубль хуже одного пропуска)."""
    with _lock:
        cur = get_conn().execute(
            "UPDATE recipients SET status='sent', sent_at=?,"
            " detail='resume: статус неоднозначен, считаем доставленным'"
            " WHERE campaign_id=? AND status='sending'",
            (_now(), campaign_id),
        )
        get_conn().commit()
        return cur.rowcount


def recipients_in_status(campaign_id: int, statuses: Iterable[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in statuses)
    return get_conn().execute(
        f"SELECT * FROM recipients WHERE campaign_id=? AND status IN ({placeholders})",
        [campaign_id, *statuses],
    ).fetchall()


def campaign_counters(campaign_id: int) -> dict[str, dict[str, int]]:
    """{bot: {status: count}} — для дашборда и HTMX-поллинга (один запрос)."""
    rows = get_conn().execute(
        "SELECT bot, status, COUNT(*) AS n FROM recipients"
        " WHERE campaign_id=? GROUP BY bot, status",
        (campaign_id,),
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["bot"], {})[r["status"]] = r["n"]
    return out


def all_recipients(campaign_id: int) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM recipients WHERE campaign_id=? ORDER BY bot, user_id",
        (campaign_id,),
    ).fetchall()


# --------------------------------------------------------- membership cache

def membership_get(chat: str, user_id: int) -> Optional[tuple[str, float]]:
    """(status, возраст записи в часах) или None. TTL применяет вызывающий:
    он асимметричный — 'member' можно кэшировать долго (протухание = лишний
    пропуск, безвредно), 'left'/'kicked' коротко (протухание = письмо тому,
    кто уже вступил в чат)."""
    row = get_conn().execute(
        "SELECT status, checked_at FROM membership_cache WHERE chat=? AND user_id=?",
        (chat, user_id),
    ).fetchone()
    if row is None:
        return None
    checked = datetime.strptime(row["checked_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - checked).total_seconds() / 3600
    return row["status"], age_hours


def membership_put(chat: str, user_id: int, status: str) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO membership_cache (chat, user_id, status, checked_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(chat, user_id) DO UPDATE SET status=excluded.status,"
            " checked_at=excluded.checked_at",
            (chat, user_id, status, _now()),
        )
        get_conn().commit()


# ------------------------------------------------------------ blocked users

def blocked_set(bot: str) -> set[int]:
    rows = get_conn().execute(
        "SELECT user_id FROM blocked_users WHERE bot=?", (bot,)
    ).fetchall()
    return {r["user_id"] for r in rows}


def add_blocked(bot: str, user_id: int) -> None:
    with _lock:
        get_conn().execute(
            "INSERT OR IGNORE INTO blocked_users (bot, user_id, blocked_at) VALUES (?, ?, ?)",
            (bot, user_id, _now()),
        )
        get_conn().commit()


# --------------------------------------------------------------- dashboard

def dashboard_stats() -> dict[str, dict[str, int]]:
    """Сводка по blocked_users для дашборда; остальное считает sources.py."""
    rows = get_conn().execute(
        "SELECT bot, COUNT(*) AS n FROM blocked_users GROUP BY bot"
    ).fetchall()
    return {r["bot"]: {"blocked": r["n"]} for r in rows}


# ------------------------------------------------------------- планирование

def schedule_campaign(campaign_id: int, scheduled_at: str, from_statuses: Iterable[str]) -> bool:
    """Атомарно ставит статус 'scheduled' и время отправки. Один и тот же
    вызов используется и для первой постановки в расписание (from_statuses
    содержит 'ready'), и для смены времени у уже запланированной
    (from_statuses = ('scheduled',)), и для немедленной отправки — тогда
    scheduled_at = now() и from_statuses включает 'ready'/'paused'."""
    placeholders = ",".join("?" for _ in from_statuses)
    with _lock:
        cur = get_conn().execute(
            f"UPDATE campaigns SET status='scheduled', scheduled_at=?"
            f" WHERE id=? AND status IN ({placeholders})",
            [scheduled_at, campaign_id, *from_statuses],
        )
        get_conn().commit()
        return cur.rowcount == 1


def unschedule_campaign(campaign_id: int) -> bool:
    with _lock:
        cur = get_conn().execute(
            "UPDATE campaigns SET status='ready', scheduled_at=NULL"
            " WHERE id=? AND status='scheduled'",
            (campaign_id,),
        )
        get_conn().commit()
        return cur.rowcount == 1


def next_due_scheduled_campaign(now: str, process_start: str) -> Optional[sqlite3.Row]:
    """Самая ранняя кампания в 'scheduled', чьё время уже настало (<=now)
    и наступило не раньше старта текущего процесса (>=process_start) —
    вторым условием просроченные ещё до рестарта кампании не подхватываются
    автоматически (см. спеку)."""
    return get_conn().execute(
        "SELECT * FROM campaigns WHERE status='scheduled' AND scheduled_at<=? AND scheduled_at>=?"
        " ORDER BY scheduled_at ASC, id ASC LIMIT 1",
        (now, process_start),
    ).fetchone()


def scheduled_campaigns() -> list[sqlite3.Row]:
    """Все запланированные кампании для страницы «Очередь», по порядку отправки."""
    return get_conn().execute(
        "SELECT * FROM campaigns WHERE status='scheduled' ORDER BY scheduled_at ASC, id ASC"
    ).fetchall()


# ---------------------------------------------------------- настройки приложения
#
# Некритичные настройки поведения живут здесь, а не в .env — правятся на
# странице «Настройки» и применяются сразу, без рестарта контейнера. В .env
# остаются только секреты (токены, пароль, ключ подписи) и то, что нужно
# ДО того, как это хранилище вообще существует (пути к БД, порт, HOST/PORT
# самого uvicorn) — их сюда переносить нельзя, курица и яйцо.
#
# Значение по умолчанию для каждой настройки — то же, что было раньше
# захардкожено в .env.example/config.py, так что апгрейд с уже
# существующей broadcast.db ничего не меняет в поведении, пока админ сам
# не поменяет значение на /settings.

def get_setting(key: str, default: str) -> str:
    row = get_conn().execute(
        "SELECT value FROM app_settings WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_setting(key: str, value: str) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        get_conn().commit()


def get_external_access() -> bool:
    """Разрешён ли вход с внешних (не локальных/туннельных) адресов.
    По умолчанию (записи нет) — выключено."""
    return get_setting("external_access", "0") == "1"


def set_external_access(enabled: bool) -> None:
    set_setting("external_access", "1" if enabled else "0")


def get_send_delay() -> float:
    return float(get_setting("send_delay", str(settings.send_delay)))


def get_member_check_delay() -> float:
    return float(get_setting("member_check_delay", str(settings.member_check_delay)))


def get_membership_ttl_hours() -> float:
    return float(get_setting("membership_ttl_hours", str(settings.membership_ttl_hours)))


def get_membership_ttl_nonmember_hours() -> float:
    return float(get_setting(
        "membership_ttl_nonmember_hours", str(settings.membership_ttl_nonmember_hours)
    ))


def get_membership_check_concurrency() -> int:
    return int(get_setting(
        "membership_check_concurrency", str(settings.membership_check_concurrency)
    ))


def get_queue_tick_seconds() -> float:
    return float(get_setting("queue_tick_seconds", str(settings.queue_tick_seconds)))


def get_exclude_chats() -> tuple[str, ...]:
    raw = get_setting("exclude_chats", ",".join(settings.exclude_chats))
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def get_admin_chat_id() -> int:
    try:
        return int(get_setting("admin_chat_id", str(settings.admin_chat_id)))
    except ValueError:
        return 0


def get_bot_label(bot: str) -> str:
    default = settings.bot_labels.get(bot, bot)
    return get_setting(f"bot_{bot.lower()}_label", default)


def get_log_level() -> str:
    return get_setting("log_level", settings.log_level)
