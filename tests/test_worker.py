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
    worker._process_start_time = ""
    yield
    worker._tasks.clear()
    worker._control.clear()
    worker._notes.clear()
    worker._process_start_time = ""


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


async def start_send_via_queue(campaign_id: int) -> None:
    """То же, что нажатие «Подтвердить отправку»/«Продолжить» плюс один тик
    обработчика очереди — так это происходит в проде (send_now ставит в
    очередь, фоновый цикл её вычитывает). Раньше тесты вызывали
    worker.start_send() напрямую; теперь start_send — внутренняя функция,
    вызывается только из _queue_tick."""
    ok = await worker.send_now(campaign_id)
    assert ok, f"не удалось поставить кампанию {campaign_id} в очередь"
    await worker._queue_tick()


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


async def test_dry_run_checks_membership_concurrently(monkeypatch):
    """Проверки членства нескольких получателей идут параллельно, а не
    строго по одному — иначе на большую аудиторию dry-run растягивается
    на десятки минут."""
    in_flight = 0
    max_in_flight = 0

    async def fake_member_status(token, chat, user_id):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", fake_member_status)
    # settings — frozen dataclass, подменяем всю ссылку на объект настроек
    import dataclasses
    monkeypatch.setattr(worker, "settings", dataclasses.replace(worker.settings, member_check_delay=0.0))

    cid = make_campaign(bots="A")  # 3 получателя в фикстуре
    assert await worker.start_dry_run(cid)
    await wait_campaign_task(cid)

    assert max_in_flight > 1
    assert db.get_campaign(cid)["status"] == "ready"


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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
    await started.wait()
    assert not await worker.send_now(cid)  # второй запуск отбит
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

    await start_send_via_queue(cid)
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
    await start_send_via_queue(cid)
    await wait_campaign_task(cid)
    assert db.get_campaign(cid)["status"] == "done"


# ----------------------------------------------------------------- очередь

async def test_queue_serializes_two_scheduled_campaigns(monkeypatch):
    """Две кампании готовы к отправке — вторая не стартует, пока не
    закончится первая, даже если обе давно due."""
    sent = []

    async def fake_send(token, chat_id, text, parse_mode=""):
        sent.append(chat_id)

    cid1 = await prepared_campaign(monkeypatch, bots="A")
    cid2 = await prepared_campaign(monkeypatch, bots="A")
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    assert await worker.send_now(cid1)
    assert await worker.send_now(cid2)

    await worker._queue_tick()  # запускает cid1 (создан раньше -> меньше id)
    assert db.get_campaign(cid1)["status"] == "running"
    assert db.get_campaign(cid2)["status"] == "scheduled"

    await worker._queue_tick()  # cid1 всё ещё занимает очередь
    assert db.get_campaign(cid2)["status"] == "scheduled"

    await wait_campaign_task(cid1)
    assert db.get_campaign(cid1)["status"] == "done"

    await worker._queue_tick()  # очередь свободна -> стартует cid2
    await wait_campaign_task(cid2)
    assert db.get_campaign(cid2)["status"] == "done"


async def test_scheduled_before_boot_not_auto_started(monkeypatch):
    """Кампания, запланированная на время до старта процесса (сервис был
    выключен) — не стартует сама, ждёт ручного решения администратора."""
    cid = await prepared_campaign(monkeypatch, bots="A")
    assert db.schedule_campaign(cid, "2000-01-01 00:00:00", ("ready",))

    await worker.resume_on_startup()  # фиксирует _process_start_time = сейчас
    await worker._queue_tick()

    assert db.get_campaign(cid)["status"] == "scheduled"


async def test_resume_paused_campaign_goes_through_queue(monkeypatch):
    """«Продолжить» после паузы идёт через очередь (paused -> scheduled ->
    running), а не переводит в running напрямую в обход сериализации."""
    async def bad_parse(token, chat_id, text, parse_mode=""):
        raise BadRequest("Bad Request: can't parse entities: unclosed tag")

    cid = await prepared_campaign(monkeypatch, bots="A", parse_mode="HTML")
    monkeypatch.setattr(worker.telegram, "send_message", bad_parse)
    await start_send_via_queue(cid)
    await wait_campaign_task(cid)  # дождаться, пока фоновая задача дойдёт до паузы
    assert db.get_campaign(cid)["status"] == "paused"

    async def ok_send(token, chat_id, text, parse_mode=""):
        pass

    monkeypatch.setattr(worker.telegram, "send_message", ok_send)
    assert await worker.send_now(cid)
    assert db.get_campaign(cid)["status"] == "scheduled"  # не running сразу

    await worker._queue_tick()
    await wait_campaign_task(cid)
    assert db.get_campaign(cid)["status"] == "done"


async def test_schedule_and_unschedule_campaign(monkeypatch):
    cid = await prepared_campaign(monkeypatch, bots="A")
    future = "2099-01-01 00:00:00"

    assert await worker.schedule_campaign(cid, future)
    row = db.get_campaign(cid)
    assert row["status"] == "scheduled"
    assert row["scheduled_at"] == future

    assert await worker.unschedule_campaign(cid)
    row = db.get_campaign(cid)
    assert row["status"] == "ready"
    assert row["scheduled_at"] is None


async def test_cancel_scheduled_campaign(monkeypatch):
    cid = await prepared_campaign(monkeypatch, bots="A")
    assert await worker.schedule_campaign(cid, "2099-01-01 00:00:00")

    assert await worker.cancel_campaign(cid)

    assert db.get_campaign(cid)["status"] == "cancelled"


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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
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

    await start_send_via_queue(cid)
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


# ---------------------------------------------------------------- картинки

async def test_send_with_image_uploads_once_then_reuses_file_id(monkeypatch, tmp_path):
    """Первому получателю картинка грузится файлом, дальше — по file_id."""
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake image bytes")

    cid = await prepared_campaign(monkeypatch, bots="A")  # 3 получателя, все pending
    db.set_campaign_image(cid, str(img))

    calls = []

    async def fake_send_photo(token, chat_id, photo, caption="", parse_mode=""):
        calls.append(photo)
        return "FILE_ID_A"

    async def must_not_send_message(*a, **k):
        raise AssertionError("для кампании с картинкой должен вызываться sendPhoto")

    monkeypatch.setattr(worker.telegram, "send_photo", fake_send_photo)
    monkeypatch.setattr(worker.telegram, "send_message", must_not_send_message)

    await start_send_via_queue(cid)
    await wait_campaign_task(cid)

    assert db.get_campaign(cid)["status"] == "done"
    st = statuses_map(cid)
    assert all(v == "sent" for v in st.values())
    assert len(calls) == 3
    assert isinstance(calls[0], tuple)          # первая — загрузка файла (имя, байты)
    assert calls[1] == "FILE_ID_A"              # дальше переиспользуем file_id
    assert calls[2] == "FILE_ID_A"


async def test_send_with_missing_image_pauses_without_burning(monkeypatch):
    """Файл картинки пропал — кампания на паузу, получатели не тронуты."""
    cid = await prepared_campaign(monkeypatch, bots="A")
    db.set_campaign_image(cid, "definitely/missing/file.png")

    sent = False

    async def fake_send_photo(*a, **k):
        nonlocal sent
        sent = True

    monkeypatch.setattr(worker.telegram, "send_photo", fake_send_photo)

    await start_send_via_queue(cid)
    await wait_campaign_task(cid)

    assert db.get_campaign(cid)["status"] == "paused"
    assert not sent
    st = statuses_map(cid)
    assert all(v == "pending" for v in st.values())
    assert "не найден" in db.get_campaign(cid)["totals_json"]


async def test_send_test_with_image(monkeypatch, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"imgdata")
    cid = make_campaign(bots="A,B")
    db.set_campaign_image(cid, str(img))

    photos = []

    async def fake_send_photo(token, chat_id, photo, caption="", parse_mode=""):
        photos.append((chat_id, photo))

    monkeypatch.setattr(worker.telegram, "send_photo", fake_send_photo)

    ok, message = await worker.send_test(cid)
    assert ok
    # admin_chat_id = 1 (conftest), оба бота, каждый грузит файл
    assert [p[0] for p in photos] == [1, 1]
    assert all(isinstance(p[1], tuple) for p in photos)
