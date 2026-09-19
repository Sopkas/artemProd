import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values

from claims_assistant.infrastructure.demo.company_data import DemoScenario


class ConfigurationError(ValueError):
    """Safe, user-facing configuration error without the rejected value."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_ids: frozenset[int]
    log_level: str = "INFO"
    # Which synthetic answer the demo provider gives; the scenario is never chosen by INN.
    demo_scenario: DemoScenario = DemoScenario.ORDINARY
    # SQLite file with runs and uploaded files; created with its directory on first start.
    database_path: Path = Path("data/claims.sqlite3")
    # Directory for uploaded .xlsx packages; created on first upload.
    storage_path: Path = Path("data/uploads")
    # Calendar date for «Сегодня» and other business dates; Moscow (UTC+3) by default.
    business_utc_offset_hours: int = 3

    data_provider: str = "demo"
    checko_api_key: str = field(default="", repr=False)

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

        raw_scenario = (values.get("DEMO_SCENARIO") or "").strip().lower() or "ordinary"
        try:
            scenario = DemoScenario(raw_scenario)
        except ValueError:
            allowed = ", ".join(item.value for item in DemoScenario)
            raise ConfigurationError(f"DEMO_SCENARIO: допустимы {allowed}.") from None
        provider = (values.get("DATA_PROVIDER") or "demo").strip().lower()
        if provider not in {"demo", "checko"}:
            raise ConfigurationError("DATA_PROVIDER: допустимы demo, checko.")
        checko_key = (values.get("CHECKO_API_KEY") or "").strip()
        if provider == "checko" and not checko_key:
            raise ConfigurationError("Для DATA_PROVIDER=checko укажите CHECKO_API_KEY.")
        database_path = _path_setting(
            values, "DATABASE_PATH", "data/claims.sqlite3", "файлу базы данных"
        )
        storage_path = _path_setting(values, "STORAGE_PATH", "data/uploads", "каталогу загрузок")
        raw_offset = (values.get("BUSINESS_UTC_OFFSET_HOURS") or "").strip() or "3"
        try:
            offset = int(raw_offset)
        except ValueError:
            raise ConfigurationError(
                "BUSINESS_UTC_OFFSET_HOURS: нужно целое число часов."
            ) from None
        if not -12 <= offset <= 14:
            raise ConfigurationError("BUSINESS_UTC_OFFSET_HOURS: допустимы значения от -12 до 14.")
        return cls(
            token=token,
            allowed_ids=allowed_ids,
            log_level=level,
            demo_scenario=scenario,
            data_provider=provider,
            checko_api_key=checko_key,
            database_path=database_path,
            storage_path=storage_path,
            business_utc_offset_hours=offset,
        )


def _path_setting(values: dict, key: str, default: str, what: str) -> Path:
    raw = values.get(key)
    if raw is None:
        return Path(default)
    if not raw.strip():
        raise ConfigurationError(f"{key}: укажите путь к {what}.")
    return Path(raw.strip())
