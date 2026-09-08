"""Все настройки сервиса — только из переменных окружения (.env).

Никаких токенов/паролей в коде. См. .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

BOT_KEYS = ("A", "B")


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    # Токены ботов (у каждого бота — свой; аудитории не смешиваются)
    bot_tokens: dict[str, str] = field(default_factory=dict)
    bot_labels: dict[str, str] = field(default_factory=dict)

    # Пути к БД
    broadcast_db_path: str = ""
    bot_a_db_path: str = ""      # bot_data.sqlite (read-only)
    bot_b_db_path: str = ""      # chat_logs.db   (read-only)

    # Каталог для загруженных картинок кампаний
    uploads_dir: str = "data/uploads"

    # Чаты-исключения (участников не рассылаем)
    exclude_chats: tuple[str, ...] = ()

    # Админка
    admin_password: str = ""
    secret_key: str = ""         # подпись cookie-сессии и CSRF
    admin_chat_id: int = 0       # тестовая отправка "на себя"

    # Лимиты
    send_delay: float = 0.08         # пауза между sendMessage, сек (~12/с)
    member_check_delay: float = 0.1  # пауза между getChatMember, сек
    # TTL кэша членства асимметричный: устаревший 'member' = лишний пропуск
    # (безвредно), устаревший 'left' = письмо уже вступившему (нежелательно)
    membership_ttl_hours: float = 24.0
    membership_ttl_nonmember_hours: float = 1.0

    host: str = "127.0.0.1"
    port: int = 8080
    # Secure-флаг сессионной cookie. По умолчанию выключен: доступ идёт через
    # SSH-туннель по http://127.0.0.1. Включить (1), если админка за HTTPS.
    cookie_secure: bool = False

    # Уровень логирования (см. app/main.py: _configure_logging). INFO нужен,
    # чтобы прогресс dry-run/отправки и resume после рестарта были видны в
    # `docker compose logs` — без этого доходят только WARNING и выше.
    log_level: str = "INFO"
    # Сколько получателей проверять на членство в exclude-чатах одновременно
    # во время dry-run (иначе проверка идёт строго по одному и на аудиторию
    # в тысячи человек может занимать десятки минут).
    membership_check_concurrency: int = 5
    # Период проверки очереди отправки (фоновый обработчик в worker.py), сек
    queue_tick_seconds: float = 5.0


def load_settings() -> Settings:
    return Settings(
        bot_tokens={
            "A": _require("TELEGRAM_BOT_TOKEN_A"),
            "B": _require("TELEGRAM_BOT_TOKEN_B"),
        },
        bot_labels={
            "A": os.environ.get("BOT_A_LABEL", "@bginfosu_bot"),
            "B": os.environ.get("BOT_B_LABEL", "@bginfosuai_bot"),
        },
        broadcast_db_path=os.environ.get("BROADCAST_DB_PATH", "data/broadcast.db"),
        uploads_dir=os.environ.get("UPLOADS_DIR", "data/uploads"),
        bot_a_db_path=_require("BOT_A_DB_PATH"),
        bot_b_db_path=_require("BOT_B_DB_PATH"),
        exclude_chats=tuple(
            c.strip() for c in os.environ.get(
                "EXCLUDE_CHATS", "@bginfosuchat,@bginfosu"
            ).split(",") if c.strip()
        ),
        admin_password=_require("ADMIN_PASSWORD"),
        secret_key=_require("SECRET_KEY"),
        admin_chat_id=int(os.environ.get("ADMIN_CHAT_ID", "0")),
        send_delay=float(os.environ.get("SEND_DELAY", "0.08")),
        member_check_delay=float(os.environ.get("MEMBER_CHECK_DELAY", "0.1")),
        membership_ttl_hours=float(os.environ.get("MEMBERSHIP_TTL_HOURS", "24")),
        membership_ttl_nonmember_hours=float(
            os.environ.get("MEMBERSHIP_TTL_NONMEMBER_HOURS", "1")
        ),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        cookie_secure=os.environ.get("COOKIE_SECURE", "0").lower() in ("1", "true", "yes"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        membership_check_concurrency=int(os.environ.get("MEMBERSHIP_CHECK_CONCURRENCY", "5")),
        queue_tick_seconds=float(os.environ.get("QUEUE_TICK_SECONDS", "5")),
    )


settings = load_settings()
