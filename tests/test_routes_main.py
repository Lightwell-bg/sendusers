"""Сквозные HTTP-тесты через TestClient: health-check, редирект без сессии,
CSRF, пагинация истории и полный жизненный цикл кампании (create → dry_run →
send → status → export.csv) через реальные роуты app/main.py — раньше этот
слой не был покрыт end-to-end (только напрямую через app/worker.py)."""

from __future__ import annotations

import re
import time

from fastapi.testclient import TestClient

from app import db, worker
from app.main import app


def _login(c: TestClient) -> None:
    r = c.post("/login", data={"password": "testpass"}, follow_redirects=False)
    c.cookies.update(r.cookies)


def _csrf(c: TestClient, path: str = "/campaigns/new") -> str:
    html = c.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "csrf-токен не найден на странице"
    return m.group(1)


def _wait_until_not_polling(c: TestClient, cid: int, timeout: float = 5.0) -> None:
    """Ждём через HTTP-слой (не через прямой db.get_campaign из другого
    потока — TestClient гоняет приложение в отдельном потоке, а sqlite3-
    соединение не безопасно для настоящего конкурентного доступа даже с
    check_same_thread=False)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = c.get(f"/campaigns/{cid}/status")
        if "HX-Refresh" in r.headers:
            return
        time.sleep(0.02)
    raise AssertionError(f"кампания {cid} не завершила выполнение за {timeout}с")


def test_health_ok():
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_protected_route_redirects_without_session():
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_post_without_csrf_is_rejected():
    with TestClient(app) as c:
        _login(c)
        r = c.post(
            "/campaigns",
            data={"title": "x", "message_text": "hi", "parse_mode": "", "bots": "A"},
        )
        assert r.status_code == 403


def test_history_and_dashboard_render():
    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        c.post(
            "/campaigns",
            data={"title": "смотровая", "message_text": "x", "parse_mode": "",
                  "bots": "A", "csrf": csrf},
            follow_redirects=False,
        )
        assert c.get("/").status_code == 200
        assert c.get("/history").status_code == 200
        assert c.get("/history?page=2").status_code == 200  # пустая, но не падает


def test_full_campaign_lifecycle_via_http(monkeypatch):
    async def all_left(token, chat, user_id):
        return "left"

    sent = []

    async def fake_send(token, chat_id, text, parse_mode=""):
        sent.append(chat_id)

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_left)
    monkeypatch.setattr(worker.telegram, "send_message", fake_send)

    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "e2e", "message_text": "Привет", "parse_mode": "",
                  "bots": "A", "csrf": csrf},
            follow_redirects=False,
        )
        assert r.status_code == 303
        cid = int(r.headers["location"].rstrip("/").split("/")[-1])
        assert db.get_campaign(cid)["status"] == "draft"  # синхронный запрос, фон ещё не запущен

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(f"/campaigns/{cid}/dry_run", data={"csrf": csrf}, follow_redirects=False)
        _wait_until_not_polling(c, cid)
        assert "Готова к отправке" in c.get(f"/campaigns/{cid}").text

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(f"/campaigns/{cid}/send", data={"csrf": csrf}, follow_redirects=False)
        _wait_until_not_polling(c, cid)
        assert "Завершена" in c.get(f"/campaigns/{cid}").text
        assert sorted(sent) == [1, 2, 3]

        status_page = c.get(f"/campaigns/{cid}/status")
        assert status_page.status_code == 200

        csv = c.get(f"/campaigns/{cid}/export.csv")
        assert csv.status_code == 200
        assert "sent" in csv.text


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


def test_schedule_rejects_bad_and_past_time(monkeypatch):
    """Мусор от браузера и время в прошлом не должны попадать в scheduled_at:
    мусор ('NaN-NaN-NaN NaN:NaN:00' от сломавшегося schedule.js) при строковом
    сравнении больше любого реального времени и кампания зависла бы в
    'scheduled' навсегда, а прошедшее время либо не сработает, либо уйдёт
    мгновенно — и то и другое молча, с одинаковым «успешным» редиректом."""
    async def all_left(token, chat, user_id):
        return "left"

    monkeypatch.setattr(worker.telegram, "get_chat_member_status", all_left)

    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "валидация", "message_text": "x", "parse_mode": "",
                  "bots": "A", "csrf": csrf},
            follow_redirects=False,
        )
        cid = int(r.headers["location"].rstrip("/").split("/")[-1])

        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(f"/campaigns/{cid}/dry_run", data={"csrf": csrf}, follow_redirects=False)
        _wait_until_not_polling(c, cid)

        for bad in ("NaN-NaN-NaN NaN:NaN:00", "завтра", "2099-01-01T00:00:00",
                    "2000-01-01 00:00:00"):
            csrf = _csrf(c, f"/campaigns/{cid}")
            r = c.post(
                f"/campaigns/{cid}/schedule",
                data={"csrf": csrf, "scheduled_at": bad},
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert "msg=" in r.headers["location"], f"{bad!r} принято молча"
            # статус не изменился — кампания осталась готовой, а не 'scheduled'
            assert c.get(f"/campaigns/{cid}").text.count("Готова к отправке") > 0

        # неполные нули strptime принимает, но в БД должен лечь канонический вид
        csrf = _csrf(c, f"/campaigns/{cid}")
        c.post(
            f"/campaigns/{cid}/schedule",
            data={"csrf": csrf, "scheduled_at": "2099-1-2 3:4:5"},
            follow_redirects=False,
        )
        assert "2099-01-02 03:04:05" in c.get("/queue").text


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


# --------------------------------------------------------------- настройки

PUBLIC_IP = "8.8.8.8"  # реальный публичный IP: RFC 5737 (203.0.113.0/24 и т.п.)
# документационные диапазоны Python ipaddress тоже относит к is_private,
# для теста нужен генуинно внешний адрес


def test_external_request_blocked_by_default():
    with TestClient(app, client=(PUBLIC_IP, 12345)) as c:
        r = c.get("/login")
        assert r.status_code == 403


def test_health_reachable_even_from_public_ip():
    with TestClient(app, client=(PUBLIC_IP, 12345)) as c:
        assert c.get("/health").status_code == 200


def test_local_client_never_blocked():
    # TestClient по умолчанию использует нераспознаваемый host "testclient" —
    # _is_private_client трактует его как доверенный (в проде ASGI всегда
    # отдаёт настоящий IP или None, см. app/main.py).
    with TestClient(app) as c:
        assert c.get("/login").status_code == 200


def test_external_access_enabled_allows_public_ip():
    with TestClient(app) as c:  # включаем с "доверенного" клиента
        _login(c)
        csrf = _csrf(c, "/settings")
        c.post(
            "/settings/external-access",
            data={"csrf": csrf, "enabled": "1"},
            follow_redirects=False,
        )
        assert db.get_external_access() is True

    with TestClient(app, client=(PUBLIC_IP, 12345)) as c:
        assert c.get("/login").status_code == 200


def test_settings_params_saved_and_validated():
    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c, "/settings")
        r = c.post(
            "/settings/params",
            data={
                "csrf": csrf,
                "send_delay": "0.5",
                "member_check_delay": "0.2",
                "membership_ttl_hours": "12",
                "membership_ttl_nonmember_hours": "2",
                "membership_check_concurrency": "3",
                "queue_tick_seconds": "10",
                "exclude_chats": "@one,@two",
                "admin_chat_id": "12345",
                "bot_a_label": "Тестбот A",
                "bot_b_label": "Тестбот B",
                "log_level": "WARNING",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert db.get_send_delay() == 0.5
        assert db.get_exclude_chats() == ("@one", "@two")
        assert db.get_bot_label("A") == "Тестбот A"
        assert db.get_log_level() == "WARNING"

        # невалидное число не должно тихо пройти и стереть сохранённое значение
        csrf = _csrf(c, "/settings")
        r = c.post(
            "/settings/params",
            data={
                "csrf": csrf,
                "send_delay": "не число",
                "member_check_delay": "0.2",
                "membership_ttl_hours": "12",
                "membership_ttl_nonmember_hours": "2",
                "membership_check_concurrency": "3",
                "queue_tick_seconds": "10",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "msg=" in r.headers["location"]
        assert db.get_send_delay() == 0.5  # прежнее значение не тронуто
