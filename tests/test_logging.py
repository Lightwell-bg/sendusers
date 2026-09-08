"""Без явной настройки логирования INFO-записи (прогресс кампаний, resume
после рестарта) молча теряются — docker compose logs показывает только
WARNING и выше. Проверяем, что app.main эту настройку действительно делает.

Уровень читается из БД (страница «Настройки»), а не из .env напрямую —
поэтому тесты выставляют его через db.set_setting, а не через settings."""

from __future__ import annotations

import logging

from app import db
from app.main import _configure_logging


def test_configure_logging_enables_info_level():
    logging.getLogger().setLevel(logging.WARNING)  # имитируем состояние "по умолчанию"
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() <= logging.INFO


def test_configure_logging_uses_db_log_level():
    db.set_setting("log_level", "WARNING")
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING

    db.set_setting("log_level", "INFO")
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_configure_logging_falls_back_to_settings_default():
    """Без записи в БД — уровень из settings.log_level (значение по
    умолчанию/из .env на момент миграции)."""
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.INFO
