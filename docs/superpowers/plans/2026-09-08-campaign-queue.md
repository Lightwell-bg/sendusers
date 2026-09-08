# Расписание и очередь отправки — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать возможность запланировать отправку кампании на конкретное время и гарантировать, что кампании отправляются строго одна за другой (очередь), а не параллельно.

**Architecture:** Новый статус кампании `scheduled` + колонка `campaigns.scheduled_at`. Ручная отправка, «Продолжить» после паузы и явное планирование — всё сводится к одному и тому же переходу `* → scheduled`. Фоновый цикл в `worker.py` тикает раз в `settings.queue_tick_seconds` секунд и, если ничего не отправляется, забирает самую раннюю готовую кампанию из `scheduled` и стартует её.

**Tech Stack:** FastAPI, Jinja2, asyncio (тот же стек, что и весь проект), без новых зависимостей.

**Spec:** `docs/superpowers/specs/2026-09-08-campaign-queue-design.md`

## Global Constraints

- Планировать можно только кампанию в статусе `ready` (dry-run уже пройден) — как и сейчас для ручной отправки.
- Одновременно может отправляться только одна кампания (проверка — по наличию кампании в статусе `running`).
- После рестарта сервиса просроченные (`scheduled_at` раньше момента старта процесса) кампании не стартуют сами — остаются в `scheduled`, видны как «Просрочено».
- Dry-run не входит в сериализацию — может идти параллельно с активной отправкой.
- Все новые SQL-запросы — только через `app/db.py` (единственное место, где модуль трогает `broadcast.db`), с сериализацией записи через существующий `threading.Lock` (`_lock`), как и весь остальной код в этом файле.
- Тесты гоняются командой `python -m pytest tests/ -q` из корня проекта (не голым `pytest` — в этом окружении он не находит пакет `app`, см. `tests/conftest.py`).
- Каждая новая настраиваемая через `.env` величина добавляется **одновременно** в `.env`, `.env.example` и таблицу переменных в `README.md` (см. существующий раздел README «Подключение сервисов и переменные окружения»).

---

## Task 1: Слой БД — статус `scheduled`, колонка `scheduled_at`, CRUD-функции очереди

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `db.now() -> str` — публичная обёртка над существующим `_now()`.
  - `db.schedule_campaign(campaign_id: int, scheduled_at: str, from_statuses: Iterable[str]) -> bool`
  - `db.unschedule_campaign(campaign_id: int) -> bool`
  - `db.next_due_scheduled_campaign(now: str, process_start: str) -> Optional[sqlite3.Row]`
  - `db.scheduled_campaigns() -> list[sqlite3.Row]`
  - Колонка `campaigns.scheduled_at` (TEXT, nullable).
  - `CAMPAIGN_STATUSES` включает `"scheduled"`.

- [ ] **Step 1: Написать падающие тесты для миграции и новых функций**

Открыть `tests/test_db.py`, вставить перед `def test_migrate_adds_image_path_to_old_db(tmp_path):` (в самом конце файла) следующие тесты:

```python
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
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `python -m pytest tests/test_db.py -k "scheduled or migrate_adds_scheduled" -v`
Expected: FAIL — `AttributeError: module 'app.db' has no attribute 'schedule_campaign'` (и аналогично для остальных новых функций).

- [ ] **Step 3: Добавить колонку в схему**

В `app/db.py` заменить блок `CREATE TABLE IF NOT EXISTS campaigns (...)` (строки 33-46):

```python
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
```

- [ ] **Step 4: Мягкая миграция для существующей БД**

В `app/db.py` заменить `_migrate` (строки 106-111):

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """Мягкие миграции для уже существующей broadcast.db (CREATE TABLE
    IF NOT EXISTS не добавляет колонки в существующую таблицу)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    if "image_path" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN image_path TEXT")
    if "scheduled_at" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN scheduled_at TEXT")
```

- [ ] **Step 5: Добавить статус `scheduled` в перечень**

В `app/db.py` заменить `CAMPAIGN_STATUSES` (строки 25-28):

```python
CAMPAIGN_STATUSES = (
    "draft", "dry_running", "ready", "scheduled", "running", "paused",
    "done", "failed", "cancelled",
)
```

- [ ] **Step 6: Публичная обёртка `now()` и новые функции очереди**

В `app/db.py` сразу после `def _now() -> str: ...` (строки 82-83) добавить:

```python
def now() -> str:
    """Публичная обёртка над _now() — нужна вызывающим за пределами этого
    модуля (worker.py), чтобы сравнивать время с scheduled_at в том же
    формате, не дублируя форматирование."""
    return _now()
```

В конец файла `app/db.py` (после `def dashboard_stats() ...`, то есть в самый конец) добавить новую секцию:

```python


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
```

- [ ] **Step 7: Прогнать тесты — должны пройти**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS — все тесты, включая новые.

- [ ] **Step 8: Прогнать весь набор тестов (регрессия)**

Run: `python -m pytest tests/ -q`
Expected: PASS (71 существующих + 9 новых = 80 passed).

- [ ] **Step 9: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "$(cat <<'EOF'
db: добавить статус scheduled и функции очереди отправки

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Слой воркера — фоновый обработчик очереди

**Files:**
- Modify: `app/config.py`
- Modify: `app/worker.py`
- Modify: `app/main.py` (одна строка в роуте `/send` — см. Step 16; остальные
  правки main.py, шаблоны и новые роуты — в Task 3)
- Modify: `tests/conftest.py`
- Modify: `tests/test_worker.py`
- Modify: `.env`, `.env.example`, `README.md` (документируем новую переменную `QUEUE_TICK_SECONDS`)

**Interfaces:**
- Consumes (из Task 1): `db.now()`, `db.schedule_campaign()`, `db.unschedule_campaign()`,
  `db.next_due_scheduled_campaign()`, `db.campaigns_in_status()` (уже существует).
- Produces:
  - `worker.schedule_campaign(campaign_id: int, scheduled_at: str, from_statuses: tuple[str, ...] = ("ready", "scheduled")) -> bool`
  - `worker.unschedule_campaign(campaign_id: int) -> bool`
  - `worker.send_now(campaign_id: int) -> bool`
  - `worker._queue_tick() -> None` (внутренняя, но напрямую вызывается тестами)
  - `worker.start_queue_processor() -> None` (запускает фоновый бесконечный цикл)
  - `worker._process_start_time: str` (module-level, "" по умолчанию)
  - `worker.start_send()` теперь требует статус `scheduled` (было `ready`/`paused`) —
    это внутренняя функция, вызывается только из `_queue_tick`.
  - `settings.queue_tick_seconds: float` (по умолчанию 5.0, `.env`: `QUEUE_TICK_SECONDS`).

### Часть А: настройка и подготовка тестов

- [ ] **Step 1: Добавить настройку периода тика в config.py**

В `app/config.py` в классе `Settings` после поля `membership_check_concurrency: int = 5` добавить:

```python
    # Период проверки очереди отправки (фоновый обработчик в worker.py), сек
    queue_tick_seconds: float = 5.0
```

В `load_settings()` после строки `membership_check_concurrency=int(os.environ.get("MEMBERSHIP_CHECK_CONCURRENCY", "5")),` добавить:

```python
        queue_tick_seconds=float(os.environ.get("QUEUE_TICK_SECONDS", "5")),
```

- [ ] **Step 2: Прописать переменную в .env.example и .env**

В `.env.example`, после строки `MEMBERSHIP_CHECK_CONCURRENCY=5` (и предшествующего комментария), добавить пустую строку и:

```
# Период проверки очереди отправки — раз в сколько секунд фоновый
# обработчик проверяет, не пора ли запускать следующую запланированную
# кампанию (не пользовательская настройка в обычном смысле, но полезно
# иметь возможность ускорить для тестов/отладки)
QUEUE_TICK_SECONDS=5
```

В `.env` (реальный, с секретами — трогать только эту вставку, остальное не менять) добавить **тот же** блок в то же место (после `MEMBERSHIP_CHECK_CONCURRENCY=5`), с тем же значением `5` (не placeholder, т.к. `.env` — не шаблон):

```
# Период проверки очереди отправки — раз в сколько секунд фоновый
# обработчик проверяет, не пора ли запускать следующую запланированную
# кампанию (не пользовательская настройка в обычном смысле, но полезно
# иметь возможность ускорить для тестов/отладки)
QUEUE_TICK_SECONDS=5
```

После правки проверить синхронность файлов:

Run: `diff <(sed -E 's/=.*/=<V>/' .env) <(sed -E 's/=.*/=<V>/' .env.example)`
Expected: пустой вывод.

- [ ] **Step 3: Задокументировать переменную в README.md**

В `README.md`, в таблице переменных окружения, после строки
`| \`MEMBERSHIP_CHECK_CONCURRENCY\` | ... |` добавить:

```
| `QUEUE_TICK_SECONDS` | Период проверки очереди отправки, сек | По умолчанию `5` |
```

- [ ] **Step 4: Ускорить тик в тестах**

В `tests/conftest.py` после строки `os.environ["UPLOADS_DIR"] = str(_TMP_DIR / "uploads")` добавить:

```python
os.environ["QUEUE_TICK_SECONDS"] = "0.05"
```

(нужно только для одного end-to-end HTTP-теста в Task 3, где реально крутится
фоновый цикл через `TestClient`; юнит-тесты воркера ниже вызывают `_queue_tick()`
напрямую и от этой настройки не зависят).

### Часть Б: миграция существующих тестов на новый API

- [ ] **Step 5: Обновить fixture очистки состояния воркера**

В `tests/test_worker.py` заменить fixture `clean_worker_state`:

```python
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
```

- [ ] **Step 6: Добавить тестовый хелпер `start_send_via_queue`**

В `tests/test_worker.py` сразу после `def statuses_map(...)` добавить:

```python
async def start_send_via_queue(campaign_id: int) -> None:
    """То же, что нажатие «Подтвердить отправку»/«Продолжить» плюс один тик
    обработчика очереди — так это происходит в проде (send_now ставит в
    очередь, фоновый цикл её вычитывает). Раньше тесты вызывали
    worker.start_send() напрямую; теперь start_send — внутренняя функция,
    вызывается только из _queue_tick."""
    ok = await worker.send_now(campaign_id)
    assert ok, f"не удалось поставить кампанию {campaign_id} в очередь"
    await worker._queue_tick()
```

- [ ] **Step 7: Заменить прямые вызовы `start_send` на хелпер**

В `tests/test_worker.py` заменить (`replace_all`) все вхождения строки:

```python
    assert await worker.start_send(cid)
```

на:

```python
    await start_send_via_queue(cid)
```

Это затронет 13 мест (тесты: `test_send_happy_path_both_bots`,
`test_send_forbidden_marks_blocked_forever`, `test_send_retry_after_no_duplicates`,
`test_send_parse_error_pauses_whole_campaign`, `test_send_per_user_error_continues`,
`test_double_start_rejected` (первое вхождение), `test_cancel_during_send`,
`test_empty_audience_finishes_immediately`, `test_send_transient_error_retried`,
`test_send_transient_error_exhausted`, `test_forbidden_permanent_only_for_real_blocks`,
`test_send_with_image_uploads_once_then_reuses_file_id`,
`test_send_with_missing_image_pauses_without_burning`).

- [ ] **Step 8: Отдельно поправить проверку двойного запуска**

В `tests/test_worker.py` в `test_double_start_rejected` заменить (это второе,
уже иначе написанное вхождение, `replace_all` его не тронул):

```python
    assert not await worker.start_send(cid)  # второй запуск отбит
```

на:

```python
    assert not await worker.send_now(cid)  # второй запуск отбит
```

(Кампания уже в статусе `running` к этому моменту — `send_now` корректно
отбивает повторную попытку, т.к. `running` не входит в допустимые
`from_statuses` для планирования.)

- [ ] **Step 9: Прогнать тесты воркера — должны падать по новой причине**

Run: `python -m pytest tests/test_worker.py -v`
Expected: FAIL — `AttributeError: module 'app.worker' has no attribute 'send_now'`
(это ожидаемо: реализация ещё не написана).

### Часть В: новые тесты на поведение очереди

- [ ] **Step 10: Добавить тесты на сериализацию, безопасность рестарта и «Продолжить»**

В `tests/test_worker.py` добавить перед секцией `# ------------------------------------------------------------ возобновление`
(перед `async def test_resume_after_crash_no_duplicates(monkeypatch):`) новую секцию:

```python
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
```

### Часть Г: реализация

- [ ] **Step 11: Добавить `_process_start_time` в реестр состояния**

В `app/worker.py` заменить блок реестров (строки 36-39):

```python
# Реестр живых задач и флаги управления ими: campaign_id -> 'pause' | 'cancel'
_tasks: dict[int, asyncio.Task] = {}
_control: dict[int, str] = {}
_notes: dict[int, str] = {}
# Момент старта текущего процесса — кампании, просроченные до этого момента
# (сервис был выключен), очередь не подхватывает автоматически, см. _queue_tick.
_process_start_time: str = ""
```

- [ ] **Step 12: Сузить `start_send` до статуса `scheduled`**

В `app/worker.py` заменить `start_send` (строки 179-186):

```python
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
```

- [ ] **Step 13: Добавить секцию «очередь» с публичными функциями**

В `app/worker.py` вставить новую секцию сразу после `_send_for_bot` (после строки 296,
перед `# ------------------------------------------------------------- управление`):

```python
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
        await _queue_tick()


def start_queue_processor() -> None:
    """Запускает бесконечный фоновый цикл — вызывать один раз при старте
    приложения (main.py:lifespan), не из тестов (тесты вызывают _queue_tick
    напрямую, без реального ожидания)."""
    asyncio.create_task(_queue_processor())


# ------------------------------------------------------------- управление
```

(Строка `# ------------------------------------------------------------- управление`
уже существует в файле — не дублировать, просто вставить новый блок перед ней.)

- [ ] **Step 14: Расширить `cancel_campaign` статусом `scheduled`**

В `app/worker.py` заменить в `cancel_campaign` (строка 319):

```python
    return db.transition_campaign(
        campaign_id,
        ("draft", "ready", "scheduled", "paused", "dry_running", "running"),
        "cancelled",
    )
```

- [ ] **Step 15: Зафиксировать момент старта процесса в `resume_on_startup`**

В `app/worker.py` заменить первую строку тела `resume_on_startup` (после докстроки,
строка 379, `for campaign in db.campaigns_in_status(("running",)):` предваряется):

```python
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
```

- [ ] **Step 16: Переключить роут `/send` на `send_now` (иначе существующий
  HTTP-тест полного цикла кампании сломается прямо на этой задаче — start_send
  из Step 12 требует статус 'scheduled', а роут в main.py пока зовёт его
  напрямую из 'ready')**

В `app/main.py` найти в `campaign_send` (роут `POST /campaigns/{campaign_id}/send`)
строку:

```python
    ok = await worker.start_send(campaign_id)
```

и заменить на:

```python
    ok = await worker.send_now(campaign_id)
```

Это тот же файл и роут, который Task 3 будет расширять дальше (формы
планирования, новые роуты `/schedule`/`/unschedule`/`/queue`) — здесь только
эта одна строка, чтобы регресс-прогон ниже не падал на уже существующем
`tests/test_routes_main.py::test_full_campaign_lifecycle_via_http`.

- [ ] **Step 17: Прогнать тесты воркера**

Run: `python -m pytest tests/test_worker.py -v`
Expected: PASS — все тесты, включая 5 новых из Step 10 и мигрированные из Step 7-8.

- [ ] **Step 18: Прогнать весь набор тестов (регрессия)**

Run: `python -m pytest tests/ -q`
Expected: PASS (80 из Task 1 + 5 новых = 85 passed). Включая уже существующий
`test_full_campaign_lifecycle_via_http` — без Step 16 он бы упал здесь.

- [ ] **Step 19: Commit**

```bash
git add app/config.py app/worker.py app/main.py tests/conftest.py tests/test_worker.py .env .env.example README.md
git commit -m "$(cat <<'EOF'
worker: фоновая очередь отправки — строго одна кампания одновременно

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Роуты, шаблоны и страница «Очередь»

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/base.html`
- Modify: `app/templates/campaign_detail.html`
- Create: `app/templates/queue.html`
- Create: `app/static/schedule.js`
- Modify: `tests/test_routes_main.py`

**Interfaces:**
- Consumes (из Task 2): `worker.schedule_campaign()`, `worker.unschedule_campaign()`,
  `worker.send_now()`, `worker.start_queue_processor()`; (из Task 1) `db.scheduled_campaigns()`, `db.now()`.
- Produces: роуты `POST /campaigns/{id}/schedule`, `POST /campaigns/{id}/unschedule`,
  `GET /queue`. (`POST /campaigns/{id}/send` уже переключён на `send_now` в Task 2 —
  здесь не трогать.)

- [ ] **Step 1: Написать падающие HTTP-тесты**

В `tests/test_routes_main.py` добавить в конец файла (после
`test_full_campaign_lifecycle_via_http`):

```python
def test_schedule_and_queue_page(monkeypatch):
    async def all_left(token, chat, user_id):
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_left)

    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "очередь", "message_text": "x", "parse_mode": "",
                  "bots": "A", "csrf": csrf},
            follow_redirects=False,
        )
        cid = int(r.headers["location"].rstrip("/").split("/")[-1])

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(f"/campaigns/{cid}/dry_run", data={"csrf": csrf}, follow_redirects=False)
        _wait_until_not_polling(c, cid)

        csrf = _csrf(c, f"/campaigns/{cid}")
        r = c.post(
            f"/campaigns/{cid}/schedule",
            data={"csrf": csrf, "scheduled_at": "2099-01-01 00:00:00"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert db.get_campaign(cid)["status"] == "scheduled"

        page = c.get("/queue")
        assert page.status_code == 200
        assert "очередь" in page.text
        assert "2099-01-01" in page.text


def test_unschedule_returns_to_ready(monkeypatch):
    async def all_left(token, chat, user_id):
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_left)

    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "u", "message_text": "x", "parse_mode": "",
                  "bots": "A", "csrf": csrf},
            follow_redirects=False,
        )
        cid = int(r.headers["location"].rstrip("/").split("/")[-1])

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(f"/campaigns/{cid}/dry_run", data={"csrf": csrf}, follow_redirects=False)
        _wait_until_not_polling(c, cid)

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(
            f"/campaigns/{cid}/schedule",
            data={"csrf": csrf, "scheduled_at": "2099-01-01 00:00:00"},
            follow_redirects=False,
        )

        csrf = _csrf(c, f"/campaigns/{cid}")
        r = c.post(f"/campaigns/{cid}/unschedule", data={"csrf": csrf}, follow_redirects=False)

        assert r.status_code == 303
        assert db.get_campaign(cid)["status"] == "ready"


def test_cancel_scheduled_campaign_via_http(monkeypatch):
    async def all_left(token, chat, user_id):
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_left)

    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "c", "message_text": "x", "parse_mode": "",
                  "bots": "A", "csrf": csrf},
            follow_redirects=False,
        )
        cid = int(r.headers["location"].rstrip("/").split("/")[-1])

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(f"/campaigns/{cid}/dry_run", data={"csrf": csrf}, follow_redirects=False)
        _wait_until_not_polling(c, cid)

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(
            f"/campaigns/{cid}/schedule",
            data={"csrf": csrf, "scheduled_at": "2099-01-01 00:00:00"},
            follow_redirects=False,
        )

        csrf = _csrf(c, f"/campaigns/{cid}")
        r = c.post(f"/campaigns/{cid}/cancel", data={"csrf": csrf}, follow_redirects=False)

        assert r.status_code == 303
        assert db.get_campaign(cid)["status"] == "cancelled"
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `python -m pytest tests/test_routes_main.py -k "schedule or unschedule or cancel_scheduled" -v`
Expected: FAIL — `404 Not Found` на `/campaigns/{id}/schedule` (роута ещё нет).

- [ ] **Step 3: STATUS_LABELS/STATUS_CLASS/POLLING_STATUSES**

В `app/main.py` заменить блок (строки 58-86):

```python
STATUS_LABELS = {
    "draft": "Черновик",
    "dry_running": "Тестовый прогон",
    "ready": "Готова к отправке",
    "scheduled": "Запланирована",
    "running": "Идёт отправка",
    "paused": "Приостановлена",
    "done": "Завершена",
    "failed": "Ошибка",
    "cancelled": "Отменена",
}
STATUS_CLASS = {
    "draft": "badge-grey",
    "dry_running": "badge-blue",
    "ready": "badge-teal",
    "scheduled": "badge-blue",
    "running": "badge-orange",
    "paused": "badge-orange",
    "done": "badge-green",
    "failed": "badge-red",
    "cancelled": "badge-red",
}
RECIPIENT_STATUS_LABELS = {
    "pending": "В очереди",
    "sending": "Отправляется",
    "sent": "Отправлено",
    "skipped_member": "Пропущен (участник исключённого чата)",
    "blocked": "Заблокировал бота",
    "error": "Ошибка",
}
POLLING_STATUSES = ("dry_running", "running", "scheduled")
```

(Добавление `"scheduled"` в `POLLING_STATUSES` — карточка кампании и статус-панель
автоматически продолжат опрашивать `/status`, пока фоновый обработчик не заберёт
кампанию в работу; без этого админу пришлось бы обновлять страницу вручную.)

- [ ] **Step 4: Запустить фоновый обработчик очереди при старте приложения**

В `app/main.py` заменить `lifespan` (строки 96-104):

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    from . import worker  # локальный импорт: модуль появится позже

    await worker.resume_on_startup()
    worker.start_queue_processor()
    yield
    db.close_db()
```

(Роут `/send` уже переключён на `worker.send_now` в Task 2, Step 16 — здесь
трогать его не нужно.)

- [ ] **Step 5: Новые роуты `/schedule`, `/unschedule`, `/queue`**

В `app/main.py` вставить после роута `campaign_send` (после строки 437, перед
`@app.post("/campaigns/{campaign_id}/cancel", ...)`):

```python
@app.post("/campaigns/{campaign_id}/schedule", dependencies=[Depends(require_auth)])
async def campaign_schedule(request: Request, campaign_id: int,
                            scheduled_at: str = Form(...), csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    if not scheduled_at.strip():
        return redirect(f"/campaigns/{campaign_id}", "Укажите время отправки")
    ok = await worker.schedule_campaign(campaign_id, scheduled_at)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.post("/campaigns/{campaign_id}/unschedule", dependencies=[Depends(require_auth)])
async def campaign_unschedule(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok = await worker.unschedule_campaign(campaign_id)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)
```

В `app/main.py` вставить после роута `history` (после строки 277, перед
`# ------------------------------------------------------------- кампании`):

```python
@app.get("/queue", dependencies=[Depends(require_auth)])
async def queue(request: Request):
    return render(
        request,
        "queue.html",
        campaigns=db.scheduled_campaigns(),
        now=db.now(),
        csrf=csrf_token(request),
    )
```

- [ ] **Step 6: JS-конвертация локального времени в UTC перед отправкой формы**

Создать `app/static/schedule.js`:

```javascript
// Конвертирует значение <input type="datetime-local"> (локальное время
// браузера) в UTC-строку "YYYY-MM-DD HH:MM:SS" перед отправкой формы —
// админ вводит своё время, не пересчитывая вручную в серверное (сервер
// работает в UTC). Форма должна иметь data-schedule-form, в ней — поле
// data-local-datetime (видимое, что вводит пользователь) и скрытое поле
// data-utc-datetime (реально уходит на сервер как scheduled_at).
(function () {
  "use strict";

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function toUtcString(localValue) {
    var d = new Date(localValue);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":00";
  }

  document.querySelectorAll("form[data-schedule-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var localInput = form.querySelector("[data-local-datetime]");
      var hidden = form.querySelector("[data-utc-datetime]");
      if (localInput && hidden && localInput.value) {
        hidden.value = toUtcString(localInput.value);
      }
    });
  });
})();
```

- [ ] **Step 7: Ссылка «Очередь» в навигации**

В `app/templates/base.html` заменить строку `<a href="/history">История</a>`:

```html
        <a href="/history">История</a>
        <a href="/queue">Очередь</a>
```

- [ ] **Step 8: Карточка кампании — форма планирования и ветка `scheduled`**

В `app/templates/campaign_detail.html` заменить ветку `{% if campaign.status == 'draft' %}`
(добавить форму планирования сразу после кнопки «Тест себе», перед ссылкой
«Редактировать») — весь блок целиком, чтобы точно попасть в контекст:

```html
    {% if campaign.status == 'draft' %}
      <form method="post" action="/campaigns/{{ campaign.id }}/dry_run" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Dry-run</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/test" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Тест себе</button>
      </form>
      <a class="button-secondary" href="/campaigns/{{ campaign.id }}/edit">Редактировать</a>

    {% elif campaign.status == 'dry_running' %}
      <form method="post" action="/campaigns/{{ campaign.id }}/cancel" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Отмена</button>
      </form>
      <p class="muted">Идёт тестовый прогон, страница обновляется автоматически…</p>

    {% elif campaign.status == 'ready' %}
      <form method="post" action="/campaigns/{{ campaign.id }}/send" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Подтвердить отправку</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/schedule" class="inline-form" data-schedule-form>
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="scheduled_at" data-utc-datetime>
        <input type="datetime-local" data-local-datetime required>
        <button type="submit">Запланировать</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/dry_run" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Dry-run заново</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/test" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Тест себе</button>
      </form>
      <a class="button-secondary" href="/campaigns/{{ campaign.id }}/edit">Редактировать</a>

    {% elif campaign.status == 'scheduled' %}
      <p class="muted">Запланирована на {{ campaign.scheduled_at }} (UTC)</p>
      <form method="post" action="/campaigns/{{ campaign.id }}/send" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Отправить сейчас</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/schedule" class="inline-form" data-schedule-form>
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="scheduled_at" data-utc-datetime>
        <input type="datetime-local" data-local-datetime required>
        <button type="submit">Изменить время</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/unschedule" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Снять с расписания</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/cancel" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Отмена</button>
      </form>

    {% elif campaign.status == 'running' %}
      <form method="post" action="/campaigns/{{ campaign.id }}/pause" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Пауза</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/cancel" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Отмена</button>
      </form>
      <p class="muted">Идёт отправка, страница обновляется автоматически…</p>

    {% elif campaign.status == 'paused' %}
      <form method="post" action="/campaigns/{{ campaign.id }}/send" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Продолжить</button>
      </form>
      <form method="post" action="/campaigns/{{ campaign.id }}/cancel" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Отмена</button>
      </form>

    {% else %}
      <a class="button-secondary" href="/campaigns/{{ campaign.id }}/export.csv">Экспорт CSV</a>
    {% endif %}
```

В конец файла `app/templates/campaign_detail.html` (после `{% endblock %}` для `content`)
добавить:

```html

{% block scripts %}
<script src="/static/schedule.js"></script>
{% endblock %}
```

- [ ] **Step 9: Создать страницу «Очередь»**

Создать `app/templates/queue.html`:

```html
{% extends "base.html" %}
{% block title %}Очередь — Админка рассылок{% endblock %}
{% block content %}
  <h1>Очередь отправки</h1>

  {% if campaigns %}
    <table class="data-table">
      <thead>
        <tr>
          <th>Время (UTC)</th>
          <th>Название</th>
          <th>Боты</th>
          <th></th>
          <th>Действия</th>
        </tr>
      </thead>
      <tbody>
        {% for c in campaigns %}
          <tr>
            <td>{{ c.scheduled_at }}</td>
            <td><a href="/campaigns/{{ c.id }}">{{ c.title }}</a></td>
            <td>{{ c.bots }}</td>
            <td>
              {% if c.scheduled_at < now %}
                <span class="badge badge-red">Просрочено</span>
              {% endif %}
            </td>
            <td>
              <form method="post" action="/campaigns/{{ c.id }}/send" class="inline-form">
                <input type="hidden" name="csrf" value="{{ csrf }}">
                <button type="submit">Отправить сейчас</button>
              </form>
              <form method="post" action="/campaigns/{{ c.id }}/schedule" class="inline-form" data-schedule-form>
                <input type="hidden" name="csrf" value="{{ csrf }}">
                <input type="hidden" name="scheduled_at" data-utc-datetime>
                <input type="datetime-local" data-local-datetime required>
                <button type="submit">Изменить время</button>
              </form>
              <form method="post" action="/campaigns/{{ c.id }}/unschedule" class="inline-form">
                <input type="hidden" name="csrf" value="{{ csrf }}">
                <button type="submit">Снять с расписания</button>
              </form>
              <a class="button-secondary" href="/campaigns/{{ c.id }}">Открыть</a>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="muted">Очередь пуста.</p>
  {% endif %}
{% endblock %}

{% block scripts %}
<script src="/static/schedule.js"></script>
{% endblock %}
```

- [ ] **Step 10: Прогнать новые тесты**

Run: `python -m pytest tests/test_routes_main.py -v`
Expected: PASS.

- [ ] **Step 11: Прогнать весь набор тестов (регрессия)**

Run: `python -m pytest tests/ -q`
Expected: PASS (85 из Task 2 + 3 новых = 88 passed). Особо проверить, что
`test_full_campaign_lifecycle_via_http` по-прежнему проходит (она теперь неявно
задействует фоновый обработчик очереди через `QUEUE_TICK_SECONDS=0.05` из
`tests/conftest.py`).

- [ ] **Step 12: Commit**

```bash
git add app/main.py app/templates/base.html app/templates/campaign_detail.html \
        app/templates/queue.html app/static/schedule.js tests/test_routes_main.py
git commit -m "$(cat <<'EOF'
Роуты, UI и страница «Очередь» для расписания отправки кампаний

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Документация и финальная проверка

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: весь функционал из Task 1-3 (документирует уже готовую фичу).

- [ ] **Step 1: Описать фичу в README.md**

В `README.md`, в разделе «Гарантии доставки (важно знать)», после существующего
пункта про единственный процесс uvicorn, добавить:

```markdown
- Отправка сериализована: в любой момент реально отправляется не больше одной
  кампании. Кампанию можно поставить в расписание (страница «Очередь») —
  фоновый обработчик забирает самую раннюю готовую, когда очередь свободна.
  Ручная отправка/«Продолжить» технически тоже становятся «запланировать на
  сейчас» и встают в ту же очередь.
- Если сервис был выключен, а время запланированной отправки уже прошло —
  она не досылается автоматически при перезапуске (появляется в очереди с
  пометкой «Просрочено»), нужно явное решение администратора.
```

- [ ] **Step 2: Финальный прогон всего набора тестов**

Run: `python -m pytest tests/ -q`
Expected: PASS, 88 passed.

- [ ] **Step 3: Ручная проверка в браузере (golden path)**

```bash
source .venv/Scripts/activate
uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

Открыть http://127.0.0.1:8080, создать кампанию → dry-run → на карточке
кампании появилось поле даты/времени и кнопка «Запланировать» → запланировать
на +2 минуты → зайти на «Очередь», убедиться, что кампания там → подождать —
статус на карточке должен смениться на «Идёт отправка», затем «Завершена»,
без ручного обновления страницы (за счёт HTMX-поллинга).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
README: задокументировать очередь и расписание отправки

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
