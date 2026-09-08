"""Тесты app/db.py — собственная база сервиса рассылок."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db


def test_create_and_get_campaign():
    cid = db.create_campaign("Заголовок", "Текст сообщения", "HTML", "A")
    row = db.get_campaign(cid)
    assert row is not None
    assert row["title"] == "Заголовок"
    assert row["message_text"] == "Текст сообщения"
    assert row["bots"] == "A"
    assert row["status"] == "draft"


def test_transition_campaign_happy_path():
    cid = db.create_campaign("T", "M", "HTML", "A")
    assert db.transition_campaign(cid, ["draft"], "dry_running") is True
    assert db.transition_campaign(cid, ["dry_running"], "ready") is True
    assert db.transition_campaign(cid, ["ready"], "running") is True
    row = db.get_campaign(cid)
    assert row["status"] == "running"
    assert row["started_at"] is not None


def test_transition_campaign_rejects_double_start():
    """Гонка: два процесса пытаются перевести кампанию ready->running.

    Только один должен победить (rowcount == 1), второй получает False.
    """
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.transition_campaign(cid, ["draft"], "dry_running")
    db.transition_campaign(cid, ["dry_running"], "ready")

    first = db.transition_campaign(cid, ["ready"], "running")
    second = db.transition_campaign(cid, ["ready"], "running")

    assert first is True
    assert second is False


def test_add_recipients_dedup():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.add_recipients(cid, "A", [(1, "a", "A"), (2, "b", "B")])
    db.add_recipients(cid, "A", [(1, "a", "A"), (3, "c", "C")])  # user_id=1 — дубль

    rows = db.all_recipients(cid)
    assert len(rows) == 3
    assert {r["user_id"] for r in rows} == {1, 2, 3}


def test_mark_recipient():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.add_recipients(cid, "A", [(1, "a", "A")])
    db.mark_recipient(cid, "A", 1, "sent")

    row = db.all_recipients(cid)[0]
    assert row["status"] == "sent"
    assert row["sent_at"] is not None


def test_campaign_counters():
    cid = db.create_campaign("T", "M", "HTML", "A,B")
    db.add_recipients(cid, "A", [(1, "a", "A"), (2, "b", "B")])
    db.add_recipients(cid, "B", [(10, "c", "C")])
    db.mark_recipient(cid, "A", 1, "sent")
    db.mark_recipient(cid, "A", 2, "error", detail="boom")
    db.mark_recipient(cid, "B", 10, "blocked")

    counters = db.campaign_counters(cid)
    assert counters["A"]["sent"] == 1
    assert counters["A"]["error"] == 1
    assert counters["B"]["blocked"] == 1


def test_membership_cache_returns_status_and_age():
    db.membership_put("@chat", 1, "member")
    result = db.membership_get("@chat", 1)
    assert result is not None
    status, age_hours = result
    assert status == "member"
    assert age_hours < 0.01  # запись только что создана


def test_membership_cache_missing_returns_none():
    assert db.membership_get("@chat", 999) is None


def test_membership_cache_ttl_expiry():
    """membership_get больше не решает за TTL — он лишь отдаёт возраст записи;
    сравнение с MEMBERSHIP_TTL_HOURS делает вызывающий код (worker.py)."""
    db.membership_put("@chat", 1, "member")

    # искусственно "состариваем" запись, как будто её проверяли двое суток назад
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_conn()
    conn.execute(
        "UPDATE membership_cache SET checked_at=? WHERE chat=? AND user_id=?",
        (old_ts, "@chat", 1),
    )
    conn.commit()

    status, age_hours = db.membership_get("@chat", 1)
    assert status == "member"
    assert age_hours >= 47.9
    # запись "устарела" относительно обычного TTL в 24 часа
    assert age_hours > 24


def test_blocked_set_and_add_blocked():
    assert db.blocked_set("A") == set()
    db.add_blocked("A", 5)
    db.add_blocked("A", 5)  # повторная вставка не должна падать (INSERT OR IGNORE)
    assert db.blocked_set("A") == {5}


def test_update_campaign_text_resets_status_and_clears_recipients():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.add_recipients(cid, "A", [(1, "a", "A")])
    db.transition_campaign(cid, ["draft"], "dry_running")
    db.transition_campaign(cid, ["dry_running"], "ready")

    ok = db.update_campaign_text(cid, "T2", "M2", "MarkdownV2", "A,B")

    assert ok is True
    row = db.get_campaign(cid)
    assert row["status"] == "draft"
    assert row["title"] == "T2"
    assert row["bots"] == "A,B"
    assert db.all_recipients(cid) == []


def test_update_campaign_text_rejected_when_running():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.transition_campaign(cid, ["draft"], "dry_running")
    db.transition_campaign(cid, ["dry_running"], "ready")
    db.transition_campaign(cid, ["ready"], "running")

    ok = db.update_campaign_text(cid, "T2", "M2", "HTML", "A")

    assert ok is False
    row = db.get_campaign(cid)
    assert row["title"] == "T"
    assert row["status"] == "running"


def test_set_campaign_image_and_get():
    cid = db.create_campaign("T", "M", "HTML", "A")
    assert db.get_campaign(cid)["image_path"] is None
    db.set_campaign_image(cid, "data/uploads/1.png")
    assert db.get_campaign(cid)["image_path"] == "data/uploads/1.png"
    db.set_campaign_image(cid, None)
    assert db.get_campaign(cid)["image_path"] is None


def test_update_campaign_text_keeps_image():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.set_campaign_image(cid, "data/uploads/1.png")
    db.update_campaign_text(cid, "T2", "M2", "HTML", "A")
    # правка текста не должна терять привязанную картинку
    assert db.get_campaign(cid)["image_path"] == "data/uploads/1.png"


def test_list_campaigns_pagination():
    ids = [db.create_campaign(f"T{i}", "M", "HTML", "A") for i in range(5)]

    page1 = db.list_campaigns(limit=2, offset=0)
    page2 = db.list_campaigns(limit=2, offset=2)

    assert [r["id"] for r in page1] == list(reversed(ids))[:2]
    assert [r["id"] for r in page2] == list(reversed(ids))[2:4]
    assert db.count_campaigns() == 5


def test_list_campaigns_without_limit_returns_all():
    for i in range(3):
        db.create_campaign(f"T{i}", "M", "HTML", "A")
    assert len(db.list_campaigns()) == 3


def test_migrate_adds_scheduled_at_to_old_db(tmp_path):
    import sqlite3

    p = tmp_path / "old2.db"
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, title TEXT, bots TEXT)"
    )
    conn.commit()
    db._migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    assert "scheduled_at" in cols
    db._migrate(conn)  # идемпотентно, без ошибки
    conn.close()


def test_schedule_campaign_sets_status_and_time():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.transition_campaign(cid, ["draft"], "dry_running")
    db.transition_campaign(cid, ["dry_running"], "ready")

    ok = db.schedule_campaign(cid, "2099-01-01 12:00:00", ("ready",))

    assert ok is True
    row = db.get_campaign(cid)
    assert row["status"] == "scheduled"
    assert row["scheduled_at"] == "2099-01-01 12:00:00"


def test_schedule_campaign_rejected_from_wrong_status():
    cid = db.create_campaign("T", "M", "HTML", "A")  # статус draft
    ok = db.schedule_campaign(cid, "2099-01-01 12:00:00", ("ready",))
    assert ok is False
    assert db.get_campaign(cid)["status"] == "draft"


def test_schedule_campaign_reschedule_updates_time():
    """Смена времени у уже запланированной — тот же вызов, from_statuses=('scheduled',)."""
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.transition_campaign(cid, ["draft"], "dry_running")
    db.transition_campaign(cid, ["dry_running"], "ready")
    db.schedule_campaign(cid, "2099-01-01 12:00:00", ("ready",))

    ok = db.schedule_campaign(cid, "2099-06-01 08:00:00", ("scheduled",))

    assert ok is True
    row = db.get_campaign(cid)
    assert row["status"] == "scheduled"
    assert row["scheduled_at"] == "2099-06-01 08:00:00"


def test_unschedule_campaign_returns_to_ready():
    cid = db.create_campaign("T", "M", "HTML", "A")
    db.transition_campaign(cid, ["draft"], "dry_running")
    db.transition_campaign(cid, ["dry_running"], "ready")
    db.schedule_campaign(cid, "2099-01-01 12:00:00", ("ready",))

    ok = db.unschedule_campaign(cid)

    assert ok is True
    row = db.get_campaign(cid)
    assert row["status"] == "ready"
    assert row["scheduled_at"] is None


def test_unschedule_campaign_rejected_when_not_scheduled():
    cid = db.create_campaign("T", "M", "HTML", "A")  # статус draft, не scheduled
    assert db.unschedule_campaign(cid) is False


def test_next_due_scheduled_campaign_respects_time_window():
    """Готова только та, чьё время уже настало И наступило не раньше старта процесса."""
    cid_due = db.create_campaign("Due", "M", "HTML", "A")
    db.transition_campaign(cid_due, ["draft"], "dry_running")
    db.transition_campaign(cid_due, ["dry_running"], "ready")
    db.schedule_campaign(cid_due, "2020-01-01 00:00:00", ("ready",))

    cid_future = db.create_campaign("Future", "M", "HTML", "A")
    db.transition_campaign(cid_future, ["draft"], "dry_running")
    db.transition_campaign(cid_future, ["dry_running"], "ready")
    db.schedule_campaign(cid_future, "2099-01-01 00:00:00", ("ready",))  # ещё не наступило

    cid_before_boot = db.create_campaign("BeforeBoot", "M", "HTML", "A")
    db.transition_campaign(cid_before_boot, ["draft"], "dry_running")
    db.transition_campaign(cid_before_boot, ["dry_running"], "ready")
    db.schedule_campaign(cid_before_boot, "1999-01-01 00:00:00", ("ready",))  # до старта процесса

    now = "2025-06-01 00:00:00"
    process_start = "2010-01-01 00:00:00"

    row = db.next_due_scheduled_campaign(now, process_start)

    assert row["id"] == cid_due


def test_next_due_scheduled_campaign_orders_by_time_then_id():
    cid_a = db.create_campaign("A", "M", "HTML", "A")
    db.transition_campaign(cid_a, ["draft"], "dry_running")
    db.transition_campaign(cid_a, ["dry_running"], "ready")
    db.schedule_campaign(cid_a, "2020-01-01 10:00:00", ("ready",))

    cid_b = db.create_campaign("B", "M", "HTML", "A")
    db.transition_campaign(cid_b, ["draft"], "dry_running")
    db.transition_campaign(cid_b, ["dry_running"], "ready")
    db.schedule_campaign(cid_b, "2020-01-01 09:00:00", ("ready",))  # раньше по времени

    row = db.next_due_scheduled_campaign("2025-01-01 00:00:00", "2010-01-01 00:00:00")

    assert row["id"] == cid_b


def test_scheduled_campaigns_lists_all_sorted():
    cid_a = db.create_campaign("A", "M", "HTML", "A")
    db.transition_campaign(cid_a, ["draft"], "dry_running")
    db.transition_campaign(cid_a, ["dry_running"], "ready")
    db.schedule_campaign(cid_a, "2020-01-01 10:00:00", ("ready",))

    cid_b = db.create_campaign("B", "M", "HTML", "A")
    db.transition_campaign(cid_b, ["draft"], "dry_running")
    db.transition_campaign(cid_b, ["dry_running"], "ready")
    db.schedule_campaign(cid_b, "2020-01-01 09:00:00", ("ready",))

    rows = db.scheduled_campaigns()

    assert [r["id"] for r in rows] == [cid_b, cid_a]


def test_migrate_adds_image_path_to_old_db(tmp_path):
    import sqlite3

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, title TEXT, bots TEXT)"
    )
    conn.commit()
    db._migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    assert "image_path" in cols
    db._migrate(conn)  # идемпотентно, без ошибки
    conn.close()
