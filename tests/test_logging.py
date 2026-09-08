"""Без явной настройки логирования INFO-записи (прогресс кампаний, resume
после рестарта) молча теряются — docker compose logs показывает только
WARNING и выше. Проверяем, что app.main эту настройку действительно делает."""

from __future__ import annotations

import dataclasses
import logging

from app.main import _configure_logging


def test_configure_logging_enables_info_level():
    logging.getLogger().setLevel(logging.WARNING)  # имитируем состояние "по умолчанию"
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() <= logging.INFO


def test_configure_logging_uses_settings_log_level(monkeypatch):
    """Settings — frozen dataclass, поэтому подменяем всю ссылку на объект
    настроек в app.main, а не отдельное поле."""
    from app import main

    warning_settings = dataclasses.replace(main.settings, log_level="WARNING")
    monkeypatch.setattr(main, "settings", warning_settings)
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING

    info_settings = dataclasses.replace(main.settings, log_level="INFO")
    monkeypatch.setattr(main, "settings", info_settings)
    _configure_logging()
