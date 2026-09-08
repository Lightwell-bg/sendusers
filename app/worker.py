"""Фоновый asyncio-воркер: dry-run (снапшот + исключения) и отправка.

Ключевые решения:
- Двухфазная отправка pending → sending → sent: commit сразу после успешного
  sendMessage; при падении между вызовом API и commit'ом на возобновлении
  зависшие 'sending' считаются доставленными (at-most-once: для маркетинговой
  рассылки дубль хуже одного пропуска).
- Внутри бота отправка последовательная с целевым интервалом
  (sleep = interval - elapsed), боты A и B работают параллельно, каждый
  строго своим токеном и только по своей аудитории.
- 429 → освободить строку, спать retry_after; 403 → blocked + в постоянный
  список; 400 «can't parse entities» — фатально для всей кампании (разметка
  общая), кампания ставится на паузу; прочие 400 — per-user error.
- Проверка членства: кэш с асимметричным TTL, short-circuit (участник первого
  чата не проверяется по второму), при ошибке — один повтор, затем fail-open
  (шлём) с пометкой в логе.
- Один процесс! Реестр задач in-process + атомарные переходы статусов в БД
  (защита от двойного запуска). С uvicorn --workers > 1 работать нельзя.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from . import db, sources, telegram
from .config import settings

logger = logging.getLogger(__name__)

# Реестр живых задач и флаги управления ими: campaign_id -> 'pause' | 'cancel'
_tasks: dict[int, asyncio.Task] = {}
_control: dict[int, str] = {}
_notes: dict[int, str] = {}
# Момент старта текущего процесса — кампании, просроченные до этого момента
# (сервис был выключен), очередь не подхватывает автоматически, см. _queue_tick.
_process_start_time: str = ""


def _task_alive(campaign_id: int) -> bool:
    task = _tasks.get(campaign_id)
    return task is not None and not task.done()


def _register(campaign_id: int, coro) -> None:
    task = asyncio.create_task(coro)
    _tasks[campaign_id] = task

    def _cleanup(t: asyncio.Task) -> None:
        _tasks.pop(campaign_id, None)
        _control.pop(campaign_id, None)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.exception("Задача кампании %s упала", campaign_id, exc_info=exc)

    task.add_done_callback(_cleanup)


def _merge_totals(campaign_id: int, patch: dict) -> None:
    row = db.get_campaign(campaign_id)
    totals = {}
    if row and row["totals_json"]:
        try:
            totals = json.loads(row["totals_json"])
        except ValueError:
            totals = {}
    totals.update(patch)
    db.set_campaign_totals(campaign_id, totals)


# ------------------------------------------------------------ membership

async def _get_member_status(bot: str, chat: str, user_id: int) -> Optional[str]:
    """Статус с одним повтором; None = не удалось узнать (fail-open)."""
    token = settings.bot_tokens[bot]
    for attempt in (1, 2):
        try:
            return await telegram.get_chat_member_status(token, chat, user_id)
        except telegram.RetryAfter as e:
            logger.info("getChatMember 429, спим %.1f c", e.retry_after)
            await asyncio.sleep(e.retry_after)
        except telegram.TelegramAPIError as e:
            logger.warning("getChatMember(%s, %s) попытка %d: %s",
                           chat, user_id, attempt, e)
            if attempt == 1:
                await asyncio.sleep(1)
    return None


async def _is_excluded(bot: str, user_id: int) -> bool:
    """Участник хотя бы одного из exclude-чатов? Short-circuit по первому."""
    for chat in settings.exclude_chats:
        cached = db.membership_get(chat, user_id)
        status: Optional[str] = None
        if cached is not None:
            cached_status, age_hours = cached
            ttl = (settings.membership_ttl_hours
                   if cached_status in telegram.MEMBER_STATUSES
                   else settings.membership_ttl_nonmember_hours)
            if age_hours <= ttl:
                status = cached_status
        if status is None:
            status = await _get_member_status(bot, chat, user_id)
            await asyncio.sleep(settings.member_check_delay)
            if status is None:
                # fail-open: не исключаем, но и не кэшируем неуспех
                logger.warning("fail-open: членство %s в %s неизвестно, шлём",
                               user_id, chat)
                continue
            db.membership_put(chat, user_id, status)
        if status in telegram.MEMBER_STATUSES:
            return True
    return False


async def _check_membership_and_mark(campaign_id: int, bot: str, row, sem: asyncio.Semaphore) -> None:
    async with sem:
        if _control.get(campaign_id) == "cancel":
            return
        if await _is_excluded(bot, row["user_id"]):
            db.mark_recipient(campaign_id, bot, row["user_id"],
                              "skipped_member", "участник exclude-чата")


# --------------------------------------------------------------- dry-run

async def start_dry_run(campaign_id: int) -> bool:
    if _task_alive(campaign_id):
        return False
    if not db.transition_campaign(campaign_id, ("draft", "ready"), "dry_running"):
        return False
    _register(campaign_id, _run_dry_run(campaign_id))
    return True


async def _run_dry_run(campaign_id: int) -> None:
    campaign = db.get_campaign(campaign_id)
    bots = [b for b in campaign["bots"].split(",") if b]
    logger.info("Dry-run кампании %s (боты: %s)", campaign_id, bots)
    try:
        db.clear_recipients(campaign_id)
        # 1. Снапшот аудитории из баз ботов (read-only)
        for bot in bots:
            users = await asyncio.to_thread(sources.fetch_users, bot)
            db.add_recipients(campaign_id, bot, users)
            # известные 403 помечаем сразу — не тратим отправку
            known_blocked = db.blocked_set(bot) & {u[0] for u in users}
            if known_blocked:
                db.mark_recipients_bulk(campaign_id, bot, known_blocked,
                                        "blocked", "ранее заблокировал бота")
        # 2. Проверка членства в exclude-чатах (один проход по снапшоту,
        # но не строго по одному — иначе на аудиторию в тысячи человек
        # dry-run растягивается на десятки минут).
        for bot in bots:
            rows = db.pending_recipients(campaign_id, bot, limit=10_000_000)
            sem = asyncio.Semaphore(settings.membership_check_concurrency)
            await asyncio.gather(
                *(_check_membership_and_mark(campaign_id, bot, row, sem) for row in rows)
            )
            if _control.get(campaign_id) == "cancel":
                db.transition_campaign(campaign_id, ("dry_running",), "cancelled")
                return
        counters = db.campaign_counters(campaign_id)
        _merge_totals(campaign_id, {"dry_run": counters})
        db.transition_campaign(campaign_id, ("dry_running",), "ready")
        logger.info("Dry-run кампании %s готов: %s", campaign_id, counters)
    except Exception as e:
        logger.exception("Dry-run кампании %s упал", campaign_id)
        _merge_totals(campaign_id, {"dry_run_error": str(e)})
        db.transition_campaign(campaign_id, ("dry_running",), "draft")


# --------------------------------------------------------------- отправка

async def start_send(campaign_id: int) -> bool:
    """Внутренняя точка входа — вызывается только из _queue_tick, когда
    очередь свободна и кампания готова (уже в статусе 'scheduled'). Не
    вызывать напрямую из роутов: сериализация держится на том, что этот
    путь — единственный, кто переводит кампанию в running."""
    if _task_alive(campaign_id):
        return False
    if not db.transition_campaign(campaign_id, ("scheduled",), "running"):
        return False
    _control.pop(campaign_id, None)
    _register(campaign_id, _run_send(campaign_id))
    return True


async def _run_send(campaign_id: int) -> None:
    campaign = db.get_campaign(campaign_id)
    bots = [b for b in campaign["bots"].split(",") if b]
    text, parse_mode = campaign["message_text"], campaign["parse_mode"]
    image_path = campaign["image_path"]
    # если картинка привязана, но файл пропал — не сжигаем аудиторию,
    # ставим на паузу с понятной пометкой
    if image_path and not os.path.exists(image_path):
        logger.error("Кампания %s: файл картинки не найден: %s", campaign_id, image_path)
        _merge_totals(campaign_id, {"send_error": f"файл картинки не найден: {image_path}"})
        db.transition_campaign(campaign_id, ("running",), "paused")
        return
    # подстраховка: зависшие 'sending' от прошлого падения считаем sent
    db.reconcile_sending(campaign_id)
    logger.info("Отправка кампании %s (боты: %s, картинка: %s)",
                campaign_id, bots, bool(image_path))
    try:
        await asyncio.gather(
            *(_send_for_bot(campaign_id, bot, text, parse_mode, image_path)
              for bot in bots)
        )
        counters = db.campaign_counters(campaign_id)
        note = _notes.pop(campaign_id, None)
        control = _control.get(campaign_id)
        if control == "cancel":
            db.transition_campaign(campaign_id, ("running",), "cancelled")
        elif control == "pause":
            db.transition_campaign(campaign_id, ("running",), "paused")
        else:
            db.transition_campaign(campaign_id, ("running",), "done")
        patch: dict = {"final": counters}
        if note:
            patch["note"] = note
        _merge_totals(campaign_id, patch)
        logger.info("Кампания %s завершена (%s): %s",
                    campaign_id, control or "done", counters)
    except Exception as e:
        logger.exception("Отправка кампании %s упала", campaign_id)
        _merge_totals(campaign_id, {"send_error": str(e)})
        db.transition_campaign(campaign_id, ("running",), "paused")


async def _send_for_bot(campaign_id: int, bot: str, text: str,
                        parse_mode: str, image_path: Optional[str] = None) -> None:
    token = settings.bot_tokens[bot]
    interval = settings.send_delay
    # счётчик повторов временных сбоев (сеть, 5xx) по каждому юзеру
    transient_tries: dict[int, int] = {}
    # file_id картинки для этого бота: первому получателю грузим файл, из
    # ответа берём file_id и переиспользуем дальше (не перезаливаем каждому).
    # file_id привязан к боту, поэтому кэш локальный для _send_for_bot.
    image_bytes: Optional[bytes] = None
    if image_path:
        image_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
    file_id: Optional[str] = None
    while True:
        if _control.get(campaign_id) in ("pause", "cancel"):
            return
        row = db.claim_next_recipient(campaign_id, bot)
        if row is None:
            return
        user_id = row["user_id"]
        t0 = time.monotonic()
        try:
            if image_path:
                photo = file_id or (os.path.basename(image_path), image_bytes)
                new_id = await telegram.send_photo(
                    token, user_id, photo, caption=text, parse_mode=parse_mode
                )
                if new_id and not file_id:
                    file_id = new_id
            else:
                await telegram.send_message(token, user_id, text, parse_mode)
            db.mark_recipient(campaign_id, bot, user_id, "sent")
        except telegram.RetryAfter as e:
            db.mark_recipient(campaign_id, bot, user_id, "pending", "retry_after")
            logger.info("Бот %s: 429, спим %.1f c", bot, e.retry_after)
            await asyncio.sleep(e.retry_after)
            continue
        except telegram.Forbidden as e:
            db.mark_recipient(campaign_id, bot, user_id, "blocked", e.description)
            # в вечный список — только заведомо необратимые причины;
            # прочие 403 блокируют юзера лишь в рамках этой кампании
            d = e.description.lower()
            if "blocked" in d or "deactivated" in d:
                db.add_blocked(bot, user_id)
        except telegram.BadRequest as e:
            if "can't parse entities" in e.description.lower():
                # ошибка разметки — общая для всех: стопим кампанию целиком
                db.mark_recipient(campaign_id, bot, user_id, "pending", None)
                _control[campaign_id] = "pause"
                _notes[campaign_id] = f"ошибка разметки: {e.description}"
                logger.error("Кампания %s: parse error, пауза: %s",
                             campaign_id, e.description)
                return
            db.mark_recipient(campaign_id, bot, user_id, "error", e.description)
        except Exception as e:  # сеть, таймауты, 5xx — повторяем до 3 раз
            tries = transient_tries.get(user_id, 0) + 1
            transient_tries[user_id] = tries
            if tries < 3:
                db.mark_recipient(campaign_id, bot, user_id, "pending",
                                  f"временный сбой, попытка {tries}")
                logger.warning("Бот %s → %s: %s (повтор %d)", bot, user_id, e, tries)
                await asyncio.sleep(2.0 * tries)
                continue
            db.mark_recipient(campaign_id, bot, user_id, "error", str(e)[:300])
            logger.warning("Бот %s → %s: %s (исчерпаны повторы)", bot, user_id, e)
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))


# ------------------------------------------------------------- очередь

async def schedule_campaign(campaign_id: int, scheduled_at: str,
                            from_statuses: tuple[str, ...] = ("ready", "scheduled")) -> bool:
    """Постановка в расписание (из 'ready') или смена времени у уже
    запланированной (from_statuses по умолчанию покрывает оба случая)."""
    ok = db.schedule_campaign(campaign_id, scheduled_at, from_statuses)
    if ok:
        logger.info("Кампания %s запланирована на %s", campaign_id, scheduled_at)
    return ok


async def send_now(campaign_id: int) -> bool:
    """«Подтвердить отправку» (из ready) / «Продолжить» (из paused) /
    «Отправить сейчас» на странице очереди (из scheduled) — все три ставят
    кампанию в очередь на текущее время вместо прямого запуска, чтобы
    никогда не обойти сериализацию отправок."""
    return await schedule_campaign(campaign_id, db.now(),
                                   from_statuses=("ready", "paused", "scheduled"))


async def unschedule_campaign(campaign_id: int) -> bool:
    ok = db.unschedule_campaign(campaign_id)
    if ok:
        logger.info("Кампания %s снята с расписания", campaign_id)
    return ok


async def _queue_tick() -> None:
    """Один тик очереди: если ничего не отправляется — забирает самую
    раннюю готовую кампанию из 'scheduled' и стартует. Кампании,
    просроченные ещё до старта этого процесса, не трогает (см.
    _process_start_time и db.next_due_scheduled_campaign)."""
    if db.campaigns_in_status(("running",)):
        return
    row = db.next_due_scheduled_campaign(db.now(), _process_start_time)
    if row is None:
        return
    await start_send(row["id"])


async def _queue_processor() -> None:
    while True:
        await asyncio.sleep(settings.queue_tick_seconds)
        try:
            await _queue_tick()
        except Exception:
            logger.exception("Тик очереди упал — продолжаю цикл")


def start_queue_processor() -> None:
    """Запускает бесконечный фоновый цикл — вызывать один раз при старте
    приложения (main.py:lifespan), не из тестов (тесты вызывают _queue_tick
    напрямую, без реального ожидания)."""
    asyncio.create_task(_queue_processor())


# ------------------------------------------------------------- управление

async def pause_campaign(campaign_id: int) -> bool:
    campaign = db.get_campaign(campaign_id)
    if campaign is None or campaign["status"] != "running":
        return False
    _control[campaign_id] = "pause"
    return True


async def cancel_campaign(campaign_id: int) -> bool:
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        return False
    if campaign["status"] in ("dry_running", "running") and _task_alive(campaign_id):
        _control[campaign_id] = "cancel"
        return True
    # задачи нет (или умерла) — переводим статус напрямую
    return db.transition_campaign(
        campaign_id,
        ("draft", "ready", "scheduled", "paused", "dry_running", "running"),
        "cancelled",
    )


async def send_test(campaign_id: int) -> tuple[bool, str]:
    """Отправка текста кампании админу каждым выбранным ботом — заодно
    проверяет разметку (parse_mode) до массовой отправки."""
    if not settings.admin_chat_id:
        return False, "ADMIN_CHAT_ID не задан в .env"
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        return False, "кампания не найдена"
    image_path = campaign["image_path"]
    if image_path and not os.path.exists(image_path):
        return False, "файл картинки не найден — перезагрузите изображение"
    image_bytes: Optional[bytes] = None
    if image_path:
        image_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
    results = []
    ok = True
    for bot in campaign["bots"].split(","):
        if not bot:
            continue
        label = settings.bot_labels.get(bot, bot)
        for attempt in (1, 2):
            try:
                if image_path:
                    await telegram.send_photo(
                        settings.bot_tokens[bot], settings.admin_chat_id,
                        (os.path.basename(image_path), image_bytes),
                        caption=campaign["message_text"],
                        parse_mode=campaign["parse_mode"],
                    )
                else:
                    await telegram.send_message(
                        settings.bot_tokens[bot], settings.admin_chat_id,
                        campaign["message_text"], campaign["parse_mode"],
                    )
                results.append(f"{label}: отправлено")
                break
            except telegram.RetryAfter as e:
                if attempt == 1 and e.retry_after <= 5:
                    await asyncio.sleep(e.retry_after)
                    continue
                ok = False
                results.append(
                    f"{label}: лимит Telegram, повторите через {e.retry_after:.0f} с"
                )
                break
            except telegram.TelegramAPIError as e:
                ok = False
                results.append(f"{label}: ошибка — {e.description}")
                break
    return ok, "; ".join(results)


async def resume_on_startup() -> None:
    """Подхват после рестарта контейнера: running-кампании продолжаются
    с pending-остатка, недоделанные dry-run перезапускаются с нуля
    (кэш членства делает повтор быстрым). Также фиксирует момент старта
    процесса — кампании, запланированные на время раньше этого момента,
    фоновая очередь не подхватывает автоматически (см. _queue_tick)."""
    global _process_start_time
    _process_start_time = db.now()
    for campaign in db.campaigns_in_status(("running",)):
        cid = campaign["id"]
        n = db.reconcile_sending(cid)
        if n:
            logger.info("Кампания %s: %d зависших 'sending' помечены sent", cid, n)
        logger.info("Возобновляю отправку кампании %s", cid)
        _register(cid, _run_send(cid))
    for campaign in db.campaigns_in_status(("dry_running",)):
        cid = campaign["id"]
        logger.info("Перезапускаю dry-run кампании %s", cid)
        _register(cid, _run_dry_run(cid))
