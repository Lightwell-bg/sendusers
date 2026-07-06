"""Тесты app/auth.py — логин, cookie-сессия, CSRF.

Намеренно тестируем хелперы напрямую (без FastAPI TestClient), чтобы не
тянуть app.main -> app.worker, которого ещё нет на момент написания этих
тестов (см. README.md).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import auth


class _DummyRequest:
    """Минимальная замена fastapi.Request — auth.py использует только .cookies."""

    def __init__(self, cookies: dict[str, str]):
        self.cookies = cookies


def test_check_password_correct():
    assert auth.check_password("testpass") is True  # ADMIN_PASSWORD из conftest.py


def test_check_password_incorrect():
    assert auth.check_password("wrong-password") is False
    assert auth.check_password("") is False


def test_session_token_round_trip():
    token = auth.make_session_token()
    assert auth.verify_session_token(token) is True


def test_session_token_rejects_garbage():
    assert auth.verify_session_token("not-a-valid-token") is False
    assert auth.verify_session_token(None) is False
    assert auth.verify_session_token("") is False


def test_session_token_expired_is_rejected(monkeypatch):
    token = auth.make_session_token()
    # Подменяем допустимый возраст токена на "отрицательный" — любой токен
    # оказывается старше допустимого, включая только что созданный.
    monkeypatch.setattr(auth, "SESSION_MAX_AGE", -1)
    assert auth.verify_session_token(token) is False


def test_require_auth_redirects_when_missing_cookie():
    request = _DummyRequest(cookies={})
    with pytest.raises(HTTPException) as exc_info:
        auth.require_auth(request)
    assert exc_info.value.status_code == 303
    assert exc_info.value.headers["Location"] == "/login"


def test_require_auth_passes_with_valid_cookie():
    token = auth.make_session_token()
    request = _DummyRequest(cookies={auth.SESSION_COOKIE: token})
    auth.require_auth(request)  # не должно бросить исключение


def test_csrf_token_round_trip():
    request = _DummyRequest(cookies={auth.SESSION_COOKIE: "session-value-123"})
    token = auth.csrf_token(request)
    auth.verify_csrf(request, token)  # не должно бросить исключение


def test_csrf_verify_rejects_mismatched_session():
    request_a = _DummyRequest(cookies={auth.SESSION_COOKIE: "session-a"})
    token = auth.csrf_token(request_a)

    request_b = _DummyRequest(cookies={auth.SESSION_COOKIE: "session-b"})
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_csrf(request_b, token)
    assert exc_info.value.status_code == 403


def test_csrf_verify_rejects_missing_token():
    request = _DummyRequest(cookies={auth.SESSION_COOKIE: "session-value"})
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_csrf(request, None)
    assert exc_info.value.status_code == 403


def test_login_rate_limit(monkeypatch):
    """После 5 неудачных попыток вход блокируется на окно, затем открывается."""
    from app import auth

    auth._failed_logins.clear()
    assert auth.login_allowed()
    for _ in range(5):
        auth.register_failed_login()
    assert not auth.login_allowed()
    # сдвигаем все попытки за пределы окна — лимит снова открыт
    monkeypatch.setattr(auth.time, "monotonic", lambda: auth._failed_logins[-1] + auth.LOGIN_WINDOW + 1)
    assert auth.login_allowed()
    auth._failed_logins.clear()
