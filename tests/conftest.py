"""Общие фикстуры для тестов.

ВАЖНО: переменные окружения должны быть выставлены ДО первого импорта любого
модуля приложения — Settings грузится один раз при импорте app.config
(``settings = load_settings()``), и другие модули делают
``from .config import settings``, поэтому после импорта поменять пути на
лету нельзя. Поэтому: пути к БД ботов и к broadcast.db фиксируются один раз
на весь тестовый сеанс (временные файлы), а между тестами таблицы
broadcast.db просто очищаются.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = Path(tempfile.mkdtemp(prefix="sendusers_test_"))

BOT_A_DB_PATH = _TMP_DIR / "bot_a.sqlite"
BOT_B_DB_PATH = _TMP_DIR / "bot_b_chat_logs.db"
BROADCAST_DB_PATH = _TMP_DIR / "broadcast.db"

os.environ["TELEGRAM_BOT_TOKEN_A"] = "dummy-token-a"
os.environ["TELEGRAM_BOT_TOKEN_B"] = "dummy-token-b"
os.environ["ADMIN_PASSWORD"] = "testpass"
os.environ["SECRET_KEY"] = "testsecret"
os.environ["ADMIN_CHAT_ID"] = "1"
os.environ["BOT_A_DB_PATH"] = str(BOT_A_DB_PATH)
os.environ["BOT_B_DB_PATH"] = str(BOT_B_DB_PATH)
os.environ["BROADCAST_DB_PATH"] = str(BROADCAST_DB_PATH)
os.environ["UPLOADS_DIR"] = str(_TMP_DIR / "uploads")


def _create_bot_a_db() -> None:
    conn = sqlite3.connect(BOT_A_DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE user_tg ("
            " user_id INTEGER PRIMARY KEY, user_name TEXT, username TEXT, timestamp TEXT)"
        )
        conn.executemany(
            "INSERT INTO user_tg (user_id, user_name, username, timestamp) VALUES (?, ?, ?, ?)",
            [
                (1, "Alice", "alice", "2026-01-01 00:00:00"),
                (2, "Bob", "bob", "2026-01-01 00:00:00"),
                (3, "Carol", None, "2026-01-01 00:00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _create_bot_b_db() -> None:
    conn = sqlite3.connect(BOT_B_DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE user_tg ("
            " user_id TEXT PRIMARY KEY, user_name TEXT, username TEXT, timestamp TEXT)"
        )
        conn.executemany(
            "INSERT INTO user_tg (user_id, user_name, username, timestamp) VALUES (?, ?, ?, ?)",
            [
                ("10", "Dave", "dave", "2026-01-01 00:00:00"),
                ("11", "Erin", "erin", "2026-01-01 00:00:00"),
                ("abc", "BadRow", "bad", "2026-01-01 00:00:00"),  # нечисловой id
                ("010", "DaveAgain", "dave2", "2026-01-01 00:00:01"),  # -> тот же int(10)
            ],
        )
        # Таблица ВКонтакте — sources.py НИКОГДА не должен её читать.
        conn.execute("CREATE TABLE user_vk (user_id INTEGER PRIMARY KEY, user_name TEXT)")
        conn.execute("INSERT INTO user_vk (user_id, user_name) VALUES (999, 'VK Leak')")
        conn.commit()
    finally:
        conn.close()


_create_bot_a_db()
_create_bot_b_db()

# Импортируем модули приложения только после того, как переменные окружения
# и файлы баз ботов уже готовы.
from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_broadcast_db():
    """Свежая схема перед первым использованием, чистые таблицы после теста."""
    db.init_db()
    yield
    conn = db.get_conn()
    conn.execute("DELETE FROM campaigns")
    conn.execute("DELETE FROM recipients")
    conn.execute("DELETE FROM membership_cache")
    conn.execute("DELETE FROM blocked_users")
    conn.commit()
