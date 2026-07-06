"""Прямой клиент Telegram Bot API: sendMessage и getChatMember.

Каждый вызов идёт токеном конкретного бота — рассылка и проверки строго
раздельны по ботам. Ошибки API переводятся в типизированные исключения,
чтобы воркер мог различать: подождать (429), пометить заблокировавшим (403),
пометить ошибкой (400 и прочее).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Статусы getChatMember, при которых пользователь считается участником
# и ИСКЛЮЧАЕТСЯ из рассылки.
MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
# Статусы, при которых пользователь НЕ участник — рассылаем.
NON_MEMBER_STATUSES = frozenset({"left", "kicked"})


class TelegramAPIError(Exception):
    """Базовая ошибка Bot API."""

    def __init__(self, description: str, error_code: Optional[int] = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code


class RetryAfter(TelegramAPIError):
    """429: Telegram просит подождать retry_after секунд."""

    def __init__(self, retry_after: float, description: str = "Too Many Requests"):
        super().__init__(description, 429)
        self.retry_after = retry_after


class Forbidden(TelegramAPIError):
    """403: пользователь заблокировал бота / бот выгнан из чата."""

    def __init__(self, description: str):
        super().__init__(description, 403)


class BadRequest(TelegramAPIError):
    """400: некорректный запрос (битая разметка, chat not found, user not found...)."""

    def __init__(self, description: str):
        super().__init__(description, 400)


class UserNotFound(BadRequest):
    """400 'user not found' в getChatMember — Telegram не знает такого юзера
    в контексте чата; трактуем как «не участник»."""


_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _call(token: str, method: str, payload: dict[str, Any]) -> Any:
    resp = await get_client().post(f"{API_BASE}/bot{token}/{method}", json=payload)
    try:
        data = resp.json()
    except ValueError as exc:
        raise TelegramAPIError(f"non-JSON ответ Telegram (HTTP {resp.status_code})") from exc

    if data.get("ok"):
        return data.get("result")

    description: str = data.get("description", "unknown error")
    error_code: int = data.get("error_code", resp.status_code)

    if error_code == 429:
        retry_after = float((data.get("parameters") or {}).get("retry_after", 5))
        raise RetryAfter(retry_after, description)
    if error_code == 403:
        raise Forbidden(description)
    if error_code == 400:
        if "user not found" in description.lower() or "participant_id_invalid" in description.lower():
            raise UserNotFound(description)
        raise BadRequest(description)
    raise TelegramAPIError(description, error_code)


async def send_message(token: str, chat_id: int, text: str,
                       parse_mode: str = "") -> None:
    """Одно сообщение одному получателю. Исключения — на усмотрение воркера."""
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    await _call(token, "sendMessage", payload)


async def get_chat_member_status(token: str, chat: str, user_id: int) -> str:
    """Статус участника чата/канала: creator|administrator|member|restricted|left|kicked.

    UserNotFound перехватываем здесь и возвращаем 'left' — Telegram не знает
    юзера в этом чате, значит рассылать можно.
    """
    try:
        result = await _call(token, "getChatMember",
                             {"chat_id": chat, "user_id": user_id})
    except UserNotFound:
        return "left"
    status = result.get("status", "unknown")
    # 'restricted' бывает и у уже вышедших: участник он или нет, говорит is_member
    if status == "restricted" and not result.get("is_member", True):
        return "left"
    return status
