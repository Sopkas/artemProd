import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values


class ConfigurationError(ValueError):
    """Safe, user-facing configuration error without the rejected value."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_ids: frozenset[int]
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: Path = Path(".env")) -> "Settings":
        try:
            values = {**dotenv_values(env_file, interpolate=False), **os.environ}
        except (OSError, UnicodeError):
            raise ConfigurationError("Не удалось прочитать локальный файл .env.") from None

        token = (values.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise ConfigurationError("Укажите TELEGRAM_BOT_TOKEN в .env или окружении.")
        try:
            validate_token(token)
        except (TokenValidationError, ValueError):
            raise ConfigurationError("Некорректный формат TELEGRAM_BOT_TOKEN.") from None

        raw_ids = (values.get("ALLOWED_TELEGRAM_IDS") or "").strip()
        if not raw_ids:
            raise ConfigurationError("Укажите хотя бы один ID в ALLOWED_TELEGRAM_IDS.")
        parts = [part.strip() for part in raw_ids.split(",")]
        if any(not re.fullmatch(r"[1-9][0-9]{0,15}", part) for part in parts):
            raise ConfigurationError(
                "ALLOWED_TELEGRAM_IDS: нужны положительные числовые ID через запятую."
            )
        allowed_ids = frozenset(int(part) for part in parts)
        if any(value >= 2**52 for value in allowed_ids):
            raise ConfigurationError("ALLOWED_TELEGRAM_IDS: ID вне допустимого диапазона.")

        level = (values.get("LOG_LEVEL") or "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL: допустимы DEBUG, INFO, WARNING, ERROR, CRITICAL.")
        return cls(token=token, allowed_ids=allowed_ids, log_level=level)
