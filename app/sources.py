"""Read-only доступ к базам данных ботов A и B.

Модуль никогда не пишет в эти базы — только SELECT в режиме read-only
(``file:...?mode=ro``). Соединение открывается и закрывается на каждый вызов.

ВАЖНО: у бота B в той же базе (chat_logs.db) есть таблица ``user_vk`` с
идентификаторами ВКонтакте — её здесь никогда не читаем.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from . import db
from .config import settings

logger = logging.getLogger(__name__)

UserRow = tuple[int, Optional[str], Optional[str]]

# Telegram user_id всегда положительный (отрицательные — чаты/каналы,
# слать туда рассылку нельзя); верхняя граница — 8-байтовый INTEGER SQLite.
MAX_TG_USER_ID = 2**63 - 1


def _valid_user_id(user_id: int) -> bool:
    return 0 < user_id <= MAX_TG_USER_ID


def _connect_ro(path: str) -> sqlite3.Connection:
    """mode=ro не защищает от "unable to open database file" на Docker
    :ro bind-маунте: SQLite даже для чтения пытается создать/проверить
    служебные файлы блокировки (wal-index и т.п.), а read-only маунт это
    запрещает на уровне ядра. immutable=1 — официальный флаг SQLite для
    базы на read-only носителе, отключает эту машинерию целиком. Каждый
    вызов этого модуля открывает свежее соединение на одну выборку и сразу
    закрывает (см. fetch_users/source_stats), поэтому не видеть чужие
    изменения, случившиеся, пока это соединение открыто, не страшно."""
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def fetch_users(bot: str) -> list[UserRow]:
    """Возвращает [(user_id, username, user_name), ...], без дублей по user_id.

    Бот A: user_id уже INTEGER PRIMARY KEY.
    Бот B: user_id хранится как TEXT — конвертируем в int, нечисловые
    значения пропускаем с предупреждением в лог.
    """
    if bot == "A":
        path = settings.bot_a_db_path
    elif bot == "B":
        path = settings.bot_b_db_path
    else:
        raise ValueError(f"Неизвестный бот: {bot!r}")

    conn = _connect_ro(path)
    try:
        rows = conn.execute(
            "SELECT user_id, username, user_name FROM user_tg"
        ).fetchall()
    finally:
        conn.close()

    users: dict[int, UserRow] = {}
    for raw_id, username, user_name in rows:
        if bot == "B":
            try:
                user_id = int(str(raw_id).strip())
            except (TypeError, ValueError):
                logger.warning(
                    "Бот B: нечисловой user_id в user_tg пропущен: %r", raw_id
                )
                continue
        else:
            user_id = int(raw_id)
        if not _valid_user_id(user_id):
            logger.warning(
                "Бот %s: user_id вне допустимого диапазона пропущен: %r", bot, raw_id
            )
            continue
        # Дедуплицируем по user_id (последняя встреченная строка побеждает).
        users[user_id] = (user_id, username, user_name)

    return list(users.values())


def source_stats() -> dict[str, dict]:
    """Сводка по каждому боту: total, unique, label, error.

    Не бросает исключений — при недоступности базы возвращает текст ошибки
    в поле ``error`` и нулевые счётчики.
    """
    stats: dict[str, dict] = {}
    for bot in ("A", "B"):
        label = db.get_bot_label(bot)
        path = settings.bot_a_db_path if bot == "A" else settings.bot_b_db_path
        entry = {"total": 0, "unique": 0, "label": label, "error": None}
        try:
            conn = _connect_ro(path)
            try:
                total = conn.execute("SELECT COUNT(*) FROM user_tg").fetchone()[0]
            finally:
                conn.close()
            users = fetch_users(bot)
            entry["total"] = total
            entry["unique"] = len(users)
        except Exception as exc:  # noqa: BLE001 — источник внешний, не даём упасть дашборду
            logger.warning("Не удалось прочитать базу бота %s (%s): %s", bot, path, exc)
            entry["error"] = str(exc)
        stats[bot] = entry
    return stats
