"""Тесты воркера: dry-run, отправка, ошибки Telegram, возобновление.

Telegram API подменяется фейками на уровне модуля app.telegram (worker
обращается к нему как worker.telegram.*). Базы ботов — фикстуры conftest:
бот A → user_id {1, 2, 3}, бот B → {10, 11} (после конвертации TEXT→int).
"""

from __future__ import annotations

import asyncio

import pytest

from app import db, worker
from app.telegram import BadRequest, Forbidden, RetryAfter


@pytest.fixture(autouse=True)
def clean_worker_state():
    worker._tasks.clear()
    worker._control.clear()
    worker._notes.clear()
    yield
    worker._tasks.clear()
    worker._control.clear()
    worker._notes.clear()


async def wait_campaign_task(campaign_id: int) -> None:
    task = worker._tasks.get(campaign_id)
    if task is not None:
        await task


def make_campaign(bots: str = "A", text: str = "Привет!", parse_mode: str = "") -> int:
    return db.create_campaign("тест", text, parse_mode, bots)


def statuses_map(campaign_id: int) -> dict[tuple[str, int], str]:
    return {
        (r["bot"], r["user_id"]): r["status"]
        for r in db.all_recipients(campaign_id)
    }


# ------------------------------------------------------------------ dry-run

async def test_dry_run_snapshot_and_exclusions(monkeypatch):
    """Снапшот из обеих баз; участник exclude-чата исключается,
    известный 403 помечается blocked ещё до отправки."""
    members = {2}  # user 2 состоит в первом exclude-чате

    async def fake_member_status(token, chat, user_id):
        return "member" if user_id in members else "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", fake_member_status)
    db.add_blocked("A", 3)  # юзер 3 когда-то заблокировал бота A

    cid = make_campaign(bots="A,B")
    assert await worker.start_dry_run(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert st[("A", 1)] == "pending"
    assert st[("A", 2)] == "skipped_member"
    assert st[("A", 3)] == "blocked"
    assert st[("B", 10)] == "pending"
    assert st[("B", 11)] == "pending"

    campaign = db.get_campaign(cid)
    assert campaign["status"] == "ready"
    assert "dry_run" in campaign["totals_json"]


async def test_dry_run_uses_membership_cache(monkeypatch):
    """Повторный dry-run не дёргает API для свежезакэшированных статусов."""
    calls = []

    async def fake_member_status(token, chat, user_id):
        calls.append((chat, user_id))
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", fake_member_status)

    cid1 = make_campaign(bots="A")
    assert await worker.start_dry_run(cid1)
    await wait_campaign_task(cid1)
    first_calls = len(calls)
    assert first_calls > 0

    cid2 = make_campaign(bots="A")
    assert await worker.start_dry_run(cid2)
    await wait_campaign_task(cid2)
    # 'left' кэшируется на membership_ttl_nonmember_hours (1ч) — повторных вызовов нет
    assert len(calls) == first_calls


async def test_dry_run_member_short_circuit(monkeypatch):
    """Участник первого чата не проверяется по второму (short-circuit)."""
    calls = []

    async def fake_member_status(token, chat, user_id):
        calls.append(chat)
        return "member"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", fake_member_status)

    cid = make_campaign(bots="A")
    assert await worker.start_dry_run(cid)
    await wait_campaign_task(cid)

    # 3 юзера бота A, каждый member первого чата → ровно 3 вызова, все по первому чату
    assert len(calls) == 3
    assert len(set(calls)) == 1


async def test_dry_run_fail_open(monkeypatch):
    """Ошибка getChatMember после повтора → fail-open: юзер остаётся pending."""

    async def fake_member_status(token, chat, user_id):
        raise worker.telegram.TelegramAPIError("Internal Server Error", 500)

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", fake_member_status)

    cid = make_campaign(bots="A")
    assert await worker.start_dry_run(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert all(v == "pending" for v in st.values())
    assert db.get_campaign(cid)["status"] == "ready"


# ----------------------------------------------------------------- отправка

async def prepared_campaign(monkeypatch, bots="A", parse_mode="") -> int:
    """Кампания после dry-run без исключений (все left)."""

    async def all_left(token, chat, user_id):
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_left)
    cid = make_campaign(bots=bots, parse_mode=parse_mode)
    assert await worker.start_dry_run(cid)
    await wait_campaign_task(cid)
    assert db.get_campaign(cid)["status"] == "ready"
    return cid


async def test_send_happy_path_both_bots(monkeypatch):
    sent = []

    async def fake_send(token, chat_id, text, parse_mode=""):
        sent.append((token, chat_id))

    cid = await prepared_campaign(monkeypatch, bots="A,B")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    campaign = db.get_campaign(cid)
    assert campaign["status"] == "done"
    st = statuses_map(cid)
    assert all(v == "sent" for v in st.values())
    # раздельность аудиторий: токен A только своим юзерам, токен B — своим
    assert ("dummy-token-a", 1) in sent and ("dummy-token-a", 10) not in sent
    assert ("dummy-token-b", 10) in sent and ("dummy-token-b", 1) not in sent
    assert "final" in campaign["totals_json"]


async def test_send_forbidden_marks_blocked_forever(monkeypatch):
    async def fake_send(token, chat_id, text, parse_mode=""):
        if chat_id == 2:
            raise Forbidden("Forbidden: bot was blocked by the user")

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert st[("A", 2)] == "blocked"
    assert st[("A", 1)] == st[("A", 3)] == "sent"
    assert 2 in db.blocked_set("A")
    assert db.get_campaign(cid)["status"] == "done"


async def test_send_retry_after_no_duplicates(monkeypatch):
    """429: строка освобождается и уходит повторно, ровно одна доставка."""
    attempts = []
    failed_once = set()

    async def fake_send(token, chat_id, text, parse_mode=""):
        attempts.append(chat_id)
        if chat_id == 2 and chat_id not in failed_once:
            failed_once.add(chat_id)
            raise RetryAfter(0.01)

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert all(v == "sent" for v in st.values())
    assert attempts.count(2) == 2  # одна неудача + одна доставка
    assert attempts.count(1) == 1 and attempts.count(3) == 1


async def test_send_parse_error_pauses_whole_campaign(monkeypatch):
    """Ошибка разметки фатальна для кампании: пауза, никто не потерян."""

    async def fake_send(token, chat_id, text, parse_mode=""):
        raise BadRequest("Bad Request: can't parse entities: unclosed tag")

    cid = await prepared_campaign(monkeypatch, bots="A", parse_mode="HTML")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    campaign = db.get_campaign(cid)
    assert campaign["status"] == "paused"
    assert "ошибка разметки" in campaign["totals_json"]
    st = statuses_map(cid)
    assert all(v == "pending" for v in st.values())  # ни один не сожжён


async def test_send_per_user_error_continues(monkeypatch):
    async def fake_send(token, chat_id, text, parse_mode=""):
        if chat_id == 2:
            raise BadRequest("Bad Request: chat not found")

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert st[("A", 2)] == "error"
    assert st[("A", 1)] == st[("A", 3)] == "sent"
    assert db.get_campaign(cid)["status"] == "done"


async def test_double_start_rejected(monkeypatch):
    started = asyncio.Event()

    async def slow_send(token, chat_id, text, parse_mode=""):
        started.set()
        await asyncio.sleep(0.3)

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", slow_send)

    assert await worker.start_send(cid)
    await started.wait()
    assert not await worker.start_send(cid)  # второй запуск отбит
    await wait_campaign_task(cid)
    assert db.get_campaign(cid)["status"] == "done"


async def test_cancel_during_send(monkeypatch):
    sent_count = 0

    async def slow_send(token, chat_id, text, parse_mode=""):
        nonlocal sent_count
        sent_count += 1
        await asyncio.sleep(0.1)

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", slow_send)

    assert await worker.start_send(cid)
    await asyncio.sleep(0.05)  # первая отправка в полёте
    assert await worker.cancel_campaign(cid)
    await wait_campaign_task(cid)

    assert db.get_campaign(cid)["status"] == "cancelled"
    assert sent_count < 3  # остановились, не дослав всех


async def test_empty_audience_finishes_immediately(monkeypatch):
    """Все исключены → running мгновенно переходит в done, без вечного бега."""

    async def all_members(token, chat, user_id):
        return "member"

    async def fake_send(token, chat_id, text, parse_mode=""):
        raise AssertionError("не должно быть отправок")

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_members)
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    cid = make_campaign(bots="A")
    assert await worker.start_dry_run(cid)
    await wait_campaign_task(cid)
    assert await worker.start_send(cid)
    await wait_campaign_task(cid)
    assert db.get_campaign(cid)["status"] == "done"


# ------------------------------------------------------------ возобновление

async def test_resume_after_crash_no_duplicates(monkeypatch):
    """Зависший 'sending' считается доставленным, pending-остаток дошёл."""
    cid = make_campaign(bots="A")
    db.add_recipients(cid, "A", [(1, "alice", "Alice"), (2, "bob", "Bob"),
                                 (3, None, "Carol")])
    # имитация падения: юзер 1 уже sent, юзер 2 завис в 'sending'
    db.mark_recipient(cid, "A", 1, "sent")
    db.get_conn().execute(
        "UPDATE recipients SET status='sending' WHERE campaign_id=? AND user_id=2", (cid,)
    )
    db.get_conn().commit()
    db.transition_campaign(cid, ("draft",), "running")

    sent = []

    async def fake_send(token, chat_id, text, parse_mode=""):
        sent.append(chat_id)

    monkeypatch.setattr(worker.telegram, "send_message", fake_send)
    await worker.resume_on_startup()
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert st == {("A", 1): "sent", ("A", 2): "sent", ("A", 3): "sent"}
    assert sent == [3]  # реально отправили только юзеру 3 — без дублей
    assert db.get_campaign(cid)["status"] == "done"


async def test_send_transient_error_retried(monkeypatch):
    """Сетевой сбой: до 3 попыток, успех со второй — юзер не сожжён."""
    attempts = {1: 0, 2: 0, 3: 0}

    async def flaky_send(token, chat_id, text, parse_mode=""):
        attempts[chat_id] += 1
        if chat_id == 2 and attempts[chat_id] < 2:
            raise OSError("connection reset")

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", flaky_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert all(v == "sent" for v in st.values())
    assert attempts[2] == 2


async def test_send_transient_error_exhausted(monkeypatch):
    """Постоянный сетевой сбой: после 3 попыток — error, кампания завершается."""

    async def broken_send(token, chat_id, text, parse_mode=""):
        if chat_id == 2:
            raise OSError("connection reset")

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", broken_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert st[("A", 2)] == "error"
    assert st[("A", 1)] == st[("A", 3)] == "sent"
    assert db.get_campaign(cid)["status"] == "done"


async def test_forbidden_permanent_only_for_real_blocks(monkeypatch):
    """403 'blocked'/'deactivated' → вечный список; иной 403 — только кампания."""

    async def fake_send(token, chat_id, text, parse_mode=""):
        if chat_id == 1:
            raise Forbidden("Forbidden: bot was blocked by the user")
        if chat_id == 2:
            raise Forbidden("Forbidden: bot can't initiate conversation with a user")

    cid = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.start_send(cid)
    await wait_campaign_task(cid)

    st = statuses_map(cid)
    assert st[("A", 1)] == "blocked" and st[("A", 2)] == "blocked"
    blocked = db.blocked_set("A")
    assert 1 in blocked and 2 not in blocked


async def test_send_test_reports_parse_error(monkeypatch):
    async def fake_send(token, chat_id, text, parse_mode=""):
        raise BadRequest("Bad Request: can't parse entities: unclosed tag")

    monkeypatch.setattr(worker.telegram, "send_message", fake_send)
    cid = make_campaign(bots="A", parse_mode="HTML")
    ok, message = await worker.send_test(cid)
    assert not ok
    assert "can't parse entities" in message
