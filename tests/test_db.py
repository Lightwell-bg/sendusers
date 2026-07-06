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
