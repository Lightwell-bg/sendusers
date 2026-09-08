"""HTTP-тесты загрузки/отдачи картинок через TestClient: полный путь
multipart-формы, отдача файла и валидация лимита подписи."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app import db
from app.main import app


def _login(c: TestClient) -> None:
    r = c.post("/login", data={"password": "testpass"}, follow_redirects=False)
    c.cookies.update(r.cookies)


def _csrf(c: TestClient, path: str = "/campaigns/new") -> str:
    html = c.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "csrf-токен не найден на странице"
    return m.group(1)


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def test_create_with_image_then_serve():
    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "с картинкой", "message_text": "подпись",
                  "parse_mode": "HTML", "bots": "A", "csrf": csrf},
            files={"image": ("pic.png", PNG, "image/png")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert "msg=" not in loc                       # без ошибки
        cid = int(loc.rstrip("/").split("/")[-1])

        campaign = db.get_campaign(cid)
        assert campaign["image_path"] and campaign["image_path"].endswith(".png")

        img = c.get(f"/campaigns/{cid}/image")
        assert img.status_code == 200
        assert img.content == PNG


def test_create_image_caption_too_long_rejected():
    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "длинная", "message_text": "x" * 1100,
                  "parse_mode": "", "bots": "A", "csrf": csrf},
            files={"image": ("pic.png", PNG, "image/png")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/campaigns/new" in r.headers["location"]   # отбито назад к форме
        assert "1024" in r.headers["location"]


def test_create_rejects_fake_image_content():
    """Расширение .png, но содержимое не похоже на картинку (нет сигнатуры) —
    отклонить сразу, а не при настоящей рассылке."""
    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "подделка", "message_text": "hi",
                  "parse_mode": "", "bots": "A", "csrf": csrf},
            files={"image": ("fake.png", b"this is not actually a png file", "image/png")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        cid = int(re.search(r"/campaigns/(\d+)", loc).group(1))
        assert db.get_campaign(cid)["image_path"] is None
        assert "msg=" in loc


def test_create_rejects_bad_extension():
    with TestClient(app) as c:
        _login(c)
        csrf = _csrf(c)
        r = c.post(
            "/campaigns",
            data={"title": "exe", "message_text": "hi",
                  "parse_mode": "", "bots": "A", "csrf": csrf},
            files={"image": ("virus.exe", b"MZ...", "application/octet-stream")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        # кампания создана, но картинка отклонена — редирект на карточку с сообщением
        cid = int(re.search(r"/campaigns/(\d+)", loc).group(1))
        assert db.get_campaign(cid)["image_path"] is None
        assert "msg=" in loc
