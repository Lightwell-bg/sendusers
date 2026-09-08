"""Аутентификация админки: пароль из .env, подписанная cookie-сессия, CSRF.

Схема простая и без внешней БД сессий:
- логин сверяет пароль через ``hmac.compare_digest``;
- при успехе выдаётся cookie ``session`` — случайный токен, подписанный
  ``itsdangerous.TimestampSigner`` (истекает через 12 часов);
- CSRF — double-submit: скрытое поле ``csrf`` в каждой POST-форме содержит
  подпись значения текущей сессионной cookie; при проверке сверяем подпись
  и совпадение с cookie.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 12 * 60 * 60  # 12 часов в секундах

# Защита от перебора пароля: не больше LOGIN_MAX_FAILURES неудачных попыток
# за LOGIN_WINDOW секунд — счётчик отдельный на каждый IP, чтобы кто угодно
# не мог намеренно "заспамить" /login чужим неверным паролем и залочить
# настоящего админа (актуально при BIND_ADDR=0.0.0.0, когда порт смотрит
# в интернет напрямую).
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 300.0
_failed_logins: dict[str, deque[float]] = defaultdict(deque)

_signer = TimestampSigner(settings.secret_key)


def _prune(client_ip: str) -> deque[float]:
    q = _failed_logins[client_ip]
    now = time.monotonic()
    while q and now - q[0] > LOGIN_WINDOW:
        q.popleft()
    if not q:
        _failed_logins.pop(client_ip, None)
    return q


def login_allowed(client_ip: str) -> bool:
    return len(_prune(client_ip)) < LOGIN_MAX_FAILURES


def register_failed_login(client_ip: str) -> None:
    q = _failed_logins[client_ip]
    q.append(time.monotonic())
    logger.warning("Неудачная попытка входа с %s (%d за последние %d с)",
                   client_ip, len(q), int(LOGIN_WINDOW))


def check_password(password: str) -> bool:
    """Безопасное (constant-time) сравнение с паролем администратора."""
    return hmac.compare_digest(
        (password or "").encode("utf-8"), settings.admin_password.encode("utf-8")
    )


def make_session_token() -> str:
    """Новый случайный токен сессии, подписанный по времени."""
    raw = secrets.token_urlsafe(32)
    return _signer.sign(raw).decode("utf-8")


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        _signer.unsign(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def is_authenticated(request: Request) -> bool:
    return verify_session_token(request.cookies.get(SESSION_COOKIE))


def require_auth(request: Request) -> None:
    """FastAPI-зависимость: если сессия невалидна — редирект на /login."""
    if not is_authenticated(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def csrf_token(request: Request) -> str:
    """CSRF-токен для текущей сессии (double-submit): подпись значения cookie."""
    session_value = request.cookies.get(SESSION_COOKIE, "")
    return _signer.sign(session_value.encode("utf-8")).decode("utf-8")


def verify_csrf(request: Request, csrf_form_value: str | None) -> None:
    """Бросает 403, если CSRF-токен формы не совпадает с сессией."""
    session_value = request.cookies.get(SESSION_COOKIE, "")
    if not csrf_form_value:
        raise HTTPException(status_code=403, detail="Отсутствует CSRF-токен")
    try:
        unsigned = _signer.unsign(csrf_form_value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")
    if unsigned.decode("utf-8") != session_value:
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")
