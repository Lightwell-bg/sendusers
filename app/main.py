"""FastAPI-приложение админки рассылок.

Маршруты см. README.md. Все страницы, кроме /login, требуют
аутентификации (см. app/auth.py); все POST-запросы проверяют CSRF-токен.

Модуль ``app.worker`` (запуск dry-run/рассылки, резюме после рестарта)
импортируется лениво внутри обработчиков — на момент написания этого
файла он ещё не существует и будет добавлен отдельно.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, sources
from .auth import (
    check_password,
    clear_session_cookie,
    csrf_token,
    login_allowed,
    make_session_token,
    register_failed_login,
    require_auth,
    set_session_cookie,
    verify_csrf,
)
from .config import settings

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Без этого INFO-логи (прогресс dry-run/отправки, resume после
    рестарта) молча теряются: без явной настройки root-логгер отдаёт в
    stderr только WARNING и выше, а именно stderr смотрит `docker compose
    logs`."""
    level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(level)


_configure_logging()

BASE_DIR = Path(__file__).resolve().parent

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
POLLING_STATUSES = ("dry_running", "scheduled", "running")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["status_label"] = lambda s: STATUS_LABELS.get(s, s)
templates.env.globals["status_class"] = lambda s: STATUS_CLASS.get(s, "badge-grey")
templates.env.globals["recipient_status_label"] = lambda s: RECIPIENT_STATUS_LABELS.get(s, s)
templates.env.globals["recipient_statuses"] = list(RECIPIENT_STATUS_LABELS.keys())
templates.env.globals["bot_labels"] = settings.bot_labels


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    from . import worker  # локальный импорт: модуль появится позже

    await worker.resume_on_startup()
    worker.start_queue_processor()
    yield
    db.close_db()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def redirect(path: str, msg: str | None = None, status_code: int = 303) -> RedirectResponse:
    if msg:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}msg={quote(msg)}"
    return RedirectResponse(path, status_code=status_code)


def render(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    return templates.TemplateResponse(request, name, ctx)


def parse_bots(bots: list[str]) -> str:
    order = [b for b in ("A", "B") if b in bots]
    return ",".join(order)


# --- планирование отправки ---

# Формат, в котором время лежит в БД (db._now()). Сравнение scheduled_at с
# текущим временем везде строковое, поэтому формат обязан совпадать
# посимвольно — иначе сравнение молча даёт неверный результат.
SCHEDULED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_scheduled_at(value: str) -> datetime | None:
    """Строгий разбор времени отправки (UTC); None — не разобралось.

    Без строгой проверки в scheduled_at попадает всё, что прислал браузер:
    например, при сбое schedule.js `new Date("")` даёт Invalid Date и строку
    "NaN-NaN-NaN NaN:NaN:00". Она непустая, поэтому проходила бы дальше, а
    при строковом сравнении оказывается больше любого реального времени —
    кампания навсегда зависает в 'scheduled' без единой ошибки в интерфейсе.
    """
    try:
        return datetime.strptime(value, SCHEDULED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --- работа с картинками кампаний ---

# Лимиты Telegram: подпись к фото 1024 символа, обычный текст 4096;
# фото до 10 МБ. Разрешаем распространённые растровые форматы.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def message_length_error(text: str, has_image: bool) -> str | None:
    limit = CAPTION_LIMIT if has_image else TEXT_LIMIT
    if len(text) > limit:
        kind = "подписи к фото" if has_image else "текста"
        return f"Превышен лимит {kind}: {len(text)} символов при максимуме {limit}"
    return None


def _upload_is_present(upload: UploadFile | None) -> bool:
    return upload is not None and bool(upload.filename)


def _looks_like_image(content: bytes) -> bool:
    """Сигнатура файла (magic bytes), а не только расширение — иначе
    переименованный не-image файл проходит форму и падает только во время
    настоящей рассылки."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if content.startswith(b"\xff\xd8\xff"):
        return True
    if content.startswith((b"GIF87a", b"GIF89a")):
        return True
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    return False


async def save_campaign_image(campaign_id: int, upload: UploadFile) -> str:
    """Сохранить загруженную картинку как data/uploads/{id}.{ext}. Возвращает
    путь. Бросает ValueError при неверном формате/размере."""
    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError(
            f"Неподдерживаемый формат {ext or '?'}. Разрешены: "
            + ", ".join(sorted(ALLOWED_IMAGE_EXT))
        )
    content = await upload.read()
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Файл слишком большой: {len(content) // 1024} КБ при лимите "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} МБ"
        )
    if not content:
        raise ValueError("Пустой файл")
    if not _looks_like_image(content):
        raise ValueError("Файл не похож на изображение (неверная сигнатура)")
    uploads = Path(settings.uploads_dir)
    uploads.mkdir(parents=True, exist_ok=True)
    # чистим ранее сохранённые варианты с другим расширением
    for old in uploads.glob(f"{campaign_id}.*"):
        old.unlink(missing_ok=True)
    dest = uploads / f"{campaign_id}{ext}"
    dest.write_bytes(content)
    return str(dest)


def remove_campaign_image(campaign_id: int, image_path: str | None) -> None:
    if image_path:
        Path(image_path).unlink(missing_ok=True)
    db.set_campaign_image(campaign_id, None)


# ------------------------------------------------------------------- login


@app.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    client_ip = request.client.host if request.client else "unknown"
    if not login_allowed(client_ip):
        return redirect("/login", "Слишком много попыток входа, подождите 5 минут")
    if not check_password(password):
        register_failed_login(client_ip)
        await asyncio.sleep(1)  # замедляем перебор
        return redirect("/login", "Неверный пароль")
    token = make_session_token()
    response = redirect("/")
    set_session_cookie(response, token)
    return response


@app.post("/logout", dependencies=[Depends(require_auth)])
async def logout(request: Request, csrf: str = Form("")):
    verify_csrf(request, csrf)
    response = redirect("/login")
    clear_session_cookie(response)
    return response


# ----------------------------------------------------------------- health


@app.get("/health")
async def health():
    """Без авторизации — только чтобы деплой-скрипт мог одной командой
    проверить, что процесс поднялся и своя база рассылок отвечает."""
    try:
        db.get_conn().execute("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="db unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------- дашборд


@app.get("/", dependencies=[Depends(require_auth)])
async def dashboard(request: Request):
    return render(
        request,
        "dashboard.html",
        source_stats=sources.source_stats(),
        blocked_stats=db.dashboard_stats(),
        recent_campaigns=db.list_campaigns(limit=10),
        csrf=csrf_token(request),
    )


HISTORY_PAGE_SIZE = 50


@app.get("/history", dependencies=[Depends(require_auth)])
async def history(request: Request, page: int = 1):
    page = max(1, page)
    total = db.count_campaigns()
    offset = (page - 1) * HISTORY_PAGE_SIZE
    campaigns = db.list_campaigns(limit=HISTORY_PAGE_SIZE, offset=offset)
    return render(
        request,
        "history.html",
        campaigns=campaigns,
        csrf=csrf_token(request),
        page=page,
        has_next=offset + HISTORY_PAGE_SIZE < total,
        has_prev=page > 1,
    )


@app.get("/queue", dependencies=[Depends(require_auth)])
async def queue(request: Request):
    from . import worker

    # process_start нужен шаблону, чтобы отличить две противоположные
    # ситуации: кампания просрочена, но ждёт освобождения очереди (норма,
    # уйдёт сама) и кампания просрочена ещё до старта процесса (сама уже
    # не уйдёт, нужны руки администратора) — см. worker._queue_tick.
    return render(
        request,
        "queue.html",
        campaigns=db.scheduled_campaigns(),
        running=db.campaigns_in_status(("running",)),
        now=db.now(),
        process_start=worker._process_start_time,
        csrf=csrf_token(request),
    )


# ------------------------------------------------------------- кампании


@app.get("/campaigns/new", dependencies=[Depends(require_auth)])
async def campaign_new_form(request: Request):
    return render(
        request,
        "campaign_new.html",
        campaign=None,
        action="/campaigns",
        csrf=csrf_token(request),
    )


@app.post("/campaigns", dependencies=[Depends(require_auth)])
async def campaign_create(
    request: Request,
    title: str = Form(...),
    message_text: str = Form(...),
    parse_mode: str = Form(""),
    bots: list[str] = Form([]),
    image: UploadFile | None = File(None),
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    bots_str = parse_bots(bots)
    if not bots_str:
        return redirect("/campaigns/new", "Выберите хотя бы одного бота")
    has_image = _upload_is_present(image)
    err = message_length_error(message_text, has_image)
    if err:
        return redirect("/campaigns/new", err)
    campaign_id = db.create_campaign(title, message_text, parse_mode, bots_str)
    if has_image:
        try:
            path = await save_campaign_image(campaign_id, image)
            db.set_campaign_image(campaign_id, path)
        except ValueError as e:
            return redirect(f"/campaigns/{campaign_id}", f"Картинка не сохранена: {e}")
    return redirect(f"/campaigns/{campaign_id}")


@app.get("/campaigns/{campaign_id}", dependencies=[Depends(require_auth)])
async def campaign_detail(request: Request, campaign_id: int):
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    counters = db.campaign_counters(campaign_id)
    totals = json.loads(campaign["totals_json"]) if campaign["totals_json"] else None
    return render(
        request,
        "campaign_detail.html",
        campaign=campaign,
        counters=counters,
        totals=totals,
        csrf=csrf_token(request),
        polling=campaign["status"] in POLLING_STATUSES,
    )


@app.get("/campaigns/{campaign_id}/edit", dependencies=[Depends(require_auth)])
async def campaign_edit_form(request: Request, campaign_id: int):
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    if campaign["status"] not in ("draft", "ready"):
        return redirect(
            f"/campaigns/{campaign_id}",
            "Редактирование доступно только для черновика или готовой кампании",
        )
    return render(
        request,
        "campaign_new.html",
        campaign=campaign,
        action=f"/campaigns/{campaign_id}/edit",
        csrf=csrf_token(request),
    )


@app.post("/campaigns/{campaign_id}/edit", dependencies=[Depends(require_auth)])
async def campaign_edit_submit(
    request: Request,
    campaign_id: int,
    title: str = Form(...),
    message_text: str = Form(...),
    parse_mode: str = Form(""),
    bots: list[str] = Form([]),
    image: UploadFile | None = File(None),
    remove_image: str = Form(""),
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    bots_str = parse_bots(bots)
    if not bots_str:
        return redirect(f"/campaigns/{campaign_id}/edit", "Выберите хотя бы одного бота")
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    new_upload = _upload_is_present(image)
    # какая картинка будет после сохранения: новая, снятая или прежняя
    will_have_image = new_upload or (bool(campaign["image_path"]) and not remove_image)
    err = message_length_error(message_text, will_have_image)
    if err:
        return redirect(f"/campaigns/{campaign_id}/edit", err)

    ok = db.update_campaign_text(campaign_id, title, message_text, parse_mode, bots_str)
    if not ok:
        return redirect(f"/campaigns/{campaign_id}", "Не удалось сохранить: недопустимый статус")
    # порядок: сначала удаление (по флагу), затем возможная новая загрузка
    if remove_image and campaign["image_path"]:
        remove_campaign_image(campaign_id, campaign["image_path"])
    if new_upload:
        try:
            path = await save_campaign_image(campaign_id, image)
            db.set_campaign_image(campaign_id, path)
        except ValueError as e:
            return redirect(f"/campaigns/{campaign_id}", f"Текст сохранён, но картинка — нет: {e}")
    return redirect(f"/campaigns/{campaign_id}", "Изменения сохранены")


@app.get("/campaigns/{campaign_id}/image", dependencies=[Depends(require_auth)])
async def campaign_image(campaign_id: int):
    campaign = db.get_campaign(campaign_id)
    if campaign is None or not campaign["image_path"]:
        raise HTTPException(status_code=404, detail="Картинка не найдена")
    path = Path(campaign["image_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл картинки отсутствует")
    return FileResponse(str(path))


@app.post("/campaigns/{campaign_id}/dry_run", dependencies=[Depends(require_auth)])
async def campaign_dry_run(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok = await worker.start_dry_run(campaign_id)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.post("/campaigns/{campaign_id}/test", dependencies=[Depends(require_auth)])
async def campaign_test(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok, message = await worker.send_test(campaign_id)
    return redirect(f"/campaigns/{campaign_id}", message)


@app.post("/campaigns/{campaign_id}/send", dependencies=[Depends(require_auth)])
async def campaign_send(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok = await worker.send_now(campaign_id)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.post("/campaigns/{campaign_id}/schedule", dependencies=[Depends(require_auth)])
async def campaign_schedule(request: Request, campaign_id: int,
                            scheduled_at: str = Form(...), csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    value = scheduled_at.strip()
    if not value:
        return redirect(f"/campaigns/{campaign_id}", "Укажите время отправки")
    when = parse_scheduled_at(value)
    if when is None:
        return redirect(
            f"/campaigns/{campaign_id}",
            "Некорректное время отправки — ожидается формат ГГГГ-ММ-ДД ЧЧ:ММ:СС (UTC)",
        )
    if when <= datetime.now(timezone.utc):
        return redirect(
            f"/campaigns/{campaign_id}",
            "Время отправки должно быть в будущем — для немедленной отправки "
            "используйте кнопку «Подтвердить отправку»/«Отправить сейчас»",
        )
    # В БД кладём канонический вид, а не то, что прислали: strptime терпит
    # неполные нули ("2099-1-1 0:0:0"), и такая строка при строковом сравнении
    # оказывается больше нормальной — кампания не ушла бы в срок.
    ok = await worker.schedule_campaign(campaign_id, when.strftime(SCHEDULED_AT_FORMAT))
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.post("/campaigns/{campaign_id}/unschedule", dependencies=[Depends(require_auth)])
async def campaign_unschedule(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok = await worker.unschedule_campaign(campaign_id)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.post("/campaigns/{campaign_id}/cancel", dependencies=[Depends(require_auth)])
async def campaign_cancel(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok = await worker.cancel_campaign(campaign_id)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.post("/campaigns/{campaign_id}/pause", dependencies=[Depends(require_auth)])
async def campaign_pause(request: Request, campaign_id: int, csrf: str = Form("")):
    verify_csrf(request, csrf)
    from . import worker

    ok = await worker.pause_campaign(campaign_id)
    msg = None if ok else "недопустимый статус"
    return redirect(f"/campaigns/{campaign_id}", msg)


@app.get("/campaigns/{campaign_id}/status", dependencies=[Depends(require_auth)])
async def campaign_status(request: Request, campaign_id: int):
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    counters = db.campaign_counters(campaign_id)
    still_polling = campaign["status"] in POLLING_STATUSES
    response = templates.TemplateResponse(
        request,
        "_status.html",
        {
            "campaign": campaign,
            "counters": counters,
            "polling": still_polling,
        },
    )
    if not still_polling:
        response.headers["HX-Refresh"] = "true"
    return response


@app.get("/campaigns/{campaign_id}/export.csv", dependencies=[Depends(require_auth)])
async def campaign_export_csv(campaign_id: int):
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")

    buffer = io.StringIO()
    buffer.write("﻿")  # BOM для корректного открытия в Excel
    writer = csv.writer(buffer)
    writer.writerow(
        ["campaign_id", "bot", "user_id", "username", "user_name", "status", "detail", "sent_at"]
    )
    for r in db.all_recipients(campaign_id):
        writer.writerow(
            [
                campaign_id,
                r["bot"],
                r["user_id"],
                r["username"],
                r["user_name"],
                r["status"],
                r["detail"],
                r["sent_at"],
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="campaign_{campaign_id}.csv"'
        },
    )
