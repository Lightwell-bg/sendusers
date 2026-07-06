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
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
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

BASE_DIR = Path(__file__).resolve().parent

STATUS_LABELS = {
    "draft": "Черновик",
    "dry_running": "Тестовый прогон",
    "ready": "Готова к отправке",
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
    "skipped_member": "Пропущен (не участник)",
    "blocked": "Заблокировал бота",
    "error": "Ошибка",
}
POLLING_STATUSES = ("dry_running", "running")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["status_label"] = lambda s: STATUS_LABELS.get(s, s)
templates.env.globals["status_class"] = lambda s: STATUS_CLASS.get(s, "badge-grey")
templates.env.globals["recipient_status_label"] = lambda s: RECIPIENT_STATUS_LABELS.get(s, s)
templates.env.globals["recipient_statuses"] = list(RECIPIENT_STATUS_LABELS.keys())
templates.env.globals["bot_labels"] = settings.bot_labels


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    from . import worker  # локальный импорт: модуль появится позже

    await worker.resume_on_startup()
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


# ------------------------------------------------------------------- login


@app.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    if not login_allowed():
        return redirect("/login", "Слишком много попыток входа, подождите 5 минут")
    if not check_password(password):
        register_failed_login()
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


# --------------------------------------------------------------- дашборд


@app.get("/", dependencies=[Depends(require_auth)])
async def dashboard(request: Request):
    return render(
        request,
        "dashboard.html",
        source_stats=sources.source_stats(),
        blocked_stats=db.dashboard_stats(),
        recent_campaigns=db.list_campaigns()[:10],
        csrf=csrf_token(request),
    )


@app.get("/history", dependencies=[Depends(require_auth)])
async def history(request: Request):
    return render(
        request, "history.html", campaigns=db.list_campaigns(), csrf=csrf_token(request)
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
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    bots_str = parse_bots(bots)
    if not bots_str:
        return redirect("/campaigns/new", "Выберите хотя бы одного бота")
    campaign_id = db.create_campaign(title, message_text, parse_mode, bots_str)
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
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    bots_str = parse_bots(bots)
    if not bots_str:
        return redirect(f"/campaigns/{campaign_id}/edit", "Выберите хотя бы одного бота")
    ok = db.update_campaign_text(campaign_id, title, message_text, parse_mode, bots_str)
    if not ok:
        return redirect(f"/campaigns/{campaign_id}", "Не удалось сохранить: недопустимый статус")
    return redirect(f"/campaigns/{campaign_id}", "Изменения сохранены")


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

    ok = await worker.start_send(campaign_id)
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
