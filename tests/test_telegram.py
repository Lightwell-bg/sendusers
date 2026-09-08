"""Юнит-тесты клиента Telegram: sendPhoto (загрузка файла и переиспользование
file_id) и классификация ошибок. HTTP-клиент подменяется фейком."""

from __future__ import annotations

import pytest

from app import telegram


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def post(self, url, json=None, data=None, files=None):
        self.calls.append({"url": url, "json": json, "data": data, "files": files})
        return FakeResponse(self.payload)


@pytest.fixture
def fake_ok_photo(monkeypatch):
    payload = {
        "ok": True,
        "result": {"photo": [{"file_id": "small"}, {"file_id": "big"}]},
    }
    client = FakeClient(payload)
    monkeypatch.setattr(telegram, "get_client", lambda: client)
    return client


async def test_send_photo_upload_returns_largest_file_id(fake_ok_photo):
    fid = await telegram.send_photo(
        "TOKEN", 123, ("pic.jpg", b"rawbytes"), caption="привет", parse_mode="HTML"
    )
    assert fid == "big"  # берём самый крупный размер
    call = fake_ok_photo.calls[-1]
    assert call["files"] is not None            # это multipart-загрузка
    assert call["data"]["chat_id"] == "123"
    assert call["data"]["caption"] == "привет"
    assert call["data"]["parse_mode"] == "HTML"


async def test_send_photo_reuse_file_id_is_json_call(fake_ok_photo):
    fid = await telegram.send_photo("TOKEN", 456, "big", caption="снова")
    assert fid == "big"
    call = fake_ok_photo.calls[-1]
    assert call["files"] is None                # переиспользование — обычный JSON
    assert call["json"]["photo"] == "big"
    assert call["json"]["caption"] == "снова"


async def test_send_photo_forbidden(monkeypatch):
    client = FakeClient({"ok": False, "error_code": 403,
                         "description": "Forbidden: bot was blocked by the user"})
    monkeypatch.setattr(telegram, "get_client", lambda: client)
    with pytest.raises(telegram.Forbidden):
        await telegram.send_photo("T", 1, ("a.png", b"x"))


async def test_send_photo_parse_error(monkeypatch):
    client = FakeClient({"ok": False, "error_code": 400,
                         "description": "Bad Request: can't parse entities"})
    monkeypatch.setattr(telegram, "get_client", lambda: client)
    with pytest.raises(telegram.BadRequest):
        await telegram.send_photo("T", 1, ("a.png", b"x"), caption="<b>bad")
