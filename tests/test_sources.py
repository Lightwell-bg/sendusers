"""Тесты app/sources.py — read-only доступ к базам ботов."""

from __future__ import annotations

import logging
from dataclasses import replace

from app import sources
from app.config import settings


def test_fetch_users_bot_a_returns_ints():
    users = sources.fetch_users("A")
    assert all(isinstance(u[0], int) for u in users)
    assert {u[0] for u in users} == {1, 2, 3}


def test_fetch_users_bot_b_converts_text_ids_and_skips_non_numeric(caplog):
    with caplog.at_level(logging.WARNING):
        users = sources.fetch_users("B")

    ids = {u[0] for u in users}
    assert all(isinstance(u[0], int) for u in users)
    assert ids == {10, 11}
    assert any("abc" in record.message for record in caplog.records)


def test_fetch_users_bot_b_dedups_by_int_id():
    # "10" и "010" в исходной базе — разные TEXT-строки, но один и тот же int(10)
    users = sources.fetch_users("B")
    ids = [u[0] for u in users]
    assert ids.count(10) == 1


def test_fetch_users_bot_b_never_leaks_user_vk():
    users = sources.fetch_users("B")
    ids = {u[0] for u in users}
    assert 999 not in ids  # 999 существует только в user_vk


def test_source_stats_missing_db_returns_error_without_raising(tmp_path, monkeypatch):
    missing_path = str(tmp_path / "does_not_exist.sqlite")
    patched_settings = replace(settings, bot_a_db_path=missing_path)
    monkeypatch.setattr(sources, "settings", patched_settings)

    stats = sources.source_stats()

    assert stats["A"]["error"] is not None
    assert stats["A"]["total"] == 0
    assert stats["A"]["unique"] == 0
    # бот B не пострадал
    assert stats["B"]["error"] is None


def test_fetch_users_skips_out_of_range_ids():
    """Отрицательные и переполняющие SQLite INTEGER id отбрасываются."""
    import sqlite3

    from app import sources
    from tests.conftest import BOT_B_DB_PATH

    conn = sqlite3.connect(BOT_B_DB_PATH)
    try:
        conn.executemany(
            "INSERT INTO user_tg (user_id, user_name, username, timestamp)"
            " VALUES (?, ?, ?, ?)",
            [
                ("-100123", "GroupLeak", "grp", "2026-01-01 00:00:00"),
                ("99999999999999999999999", "Overflow", "ovf", "2026-01-01 00:00:00"),
            ],
        )
        conn.commit()
        ids = {u[0] for u in sources.fetch_users("B")}
        assert ids == {10, 11}
    finally:
        conn.execute("DELETE FROM user_tg WHERE user_id IN ('-100123', '99999999999999999999999')")
        conn.commit()
        conn.close()
