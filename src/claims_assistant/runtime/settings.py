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
        return cls(
            token=token,
            allowed_ids=allowed_ids,
            log_level=level,
            demo_scenario=scenario,
            data_provider=provider,
            checko_api_key=checko_key,
        )
