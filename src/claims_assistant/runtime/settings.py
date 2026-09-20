import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values

from claims_assistant.infrastructure.demo.company_data import DemoScenario


@dataclass(frozen=True)
class ExternalLimits:
    """Limits for external sources; defaults are the architecture's starting values,
    to be aligned with the real Checko tariff (S3-02)."""

    timeout_seconds: float = 20.0
    max_retries: int = 2
    cache_ttl_seconds: float = 3600.0
    run_time_limit_seconds: float = 900.0
    run_request_limit: int = 1500


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
    external: ExternalLimits = ExternalLimits()
    # Calendar date for «Сегодня» and other business dates; Moscow (UTC+3) by default.
    business_utc_offset_hours: int = 3
    # Retention (S6-02): finished and never-launched checks older than this are deleted
    # with their files; the sweep runs at start and then every interval.
    retention_days: int = 30
    retention_sweep_seconds: float = 6 * 3600
    # Backups (S6-02): where `python -m claims_assistant.ops backup` puts snapshots and
    # how many newest snapshots it keeps.
    backup_path: Path = Path("data/backups")
    backup_keep: int = 7

    data_provider: str = "demo"
    checko_api_key: str = field(default="", repr=False)
    # AI explanations (S5-01): "off" — no provider at all; "stub" — the stand-in model
    # (no network). A real provider is added only after the customer confirms it.
    ai_provider: str = "off"
    # Whether the «Взаимодействия» comments may be sent to the model (S5-02/S5-05):
    # the customer's employees' and clients' words leave the premises only with consent.
    ai_send_comments: bool = False

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
        ai_provider = (values.get("AI_PROVIDER") or "off").strip().lower()
        if ai_provider not in {"off", "stub"}:
            raise ConfigurationError("AI_PROVIDER: допустимы off, stub.")
        raw_comments = (values.get("AI_SEND_COMMENTS") or "false").strip().lower()
        if raw_comments not in {"true", "false", "1", "0", "yes", "no"}:
            raise ConfigurationError("AI_SEND_COMMENTS: допустимы true или false.")
        ai_send_comments = raw_comments in {"true", "1", "yes"}
        checko_key = (values.get("CHECKO_API_KEY") or "").strip()
        if provider == "checko" and not checko_key:
            raise ConfigurationError("Для DATA_PROVIDER=checko укажите CHECKO_API_KEY.")
        database_path = _path_setting(
            values, "DATABASE_PATH", "data/claims.sqlite3", "файлу базы данных"
        )
        storage_path = _path_setting(values, "STORAGE_PATH", "data/uploads", "каталогу загрузок")
        external = ExternalLimits(
            timeout_seconds=_number(values, "EXTERNAL_TIMEOUT_SECONDS", 20.0, minimum=0.001),
            max_retries=_number(values, "EXTERNAL_MAX_RETRIES", 2, integer=True, minimum=0),
            cache_ttl_seconds=_number(values, "EXTERNAL_CACHE_TTL_SECONDS", 3600.0, minimum=0),
            run_time_limit_seconds=_number(values, "RUN_TIME_LIMIT_SECONDS", 900.0, minimum=1),
            run_request_limit=_number(values, "RUN_REQUEST_LIMIT", 1500, integer=True, minimum=1),
        )
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
            ai_provider=ai_provider,
            ai_send_comments=ai_send_comments,
            database_path=database_path,
            storage_path=storage_path,
            external=external,
            business_utc_offset_hours=offset,
            retention_days=_number(values, "RETENTION_DAYS", 30, integer=True, minimum=1),
            retention_sweep_seconds=_number(
                values, "RETENTION_SWEEP_SECONDS", 6 * 3600.0, minimum=60
            ),
            backup_path=_path_setting(values, "BACKUP_PATH", "data/backups", "каталогу копий"),
            backup_keep=_number(values, "BACKUP_KEEP", 7, integer=True, minimum=1),
        )


def _path_setting(values: dict, key: str, default: str, what: str) -> Path:
    raw = values.get(key)
    if raw is None:
        return Path(default)
    if not raw.strip():
        raise ConfigurationError(f"{key}: укажите путь к {what}.")
    return Path(raw.strip())


def _number(values: dict, key: str, default, *, integer: bool = False, minimum):
    raw = values.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        number = int(raw.strip()) if integer else float(raw.strip())
    except ValueError:
        kind = "целое число" if integer else "число"
        raise ConfigurationError(f"{key}: нужно {kind}.") from None
    if number < minimum or (not integer and number != number):
        raise ConfigurationError(f"{key}: значение не меньше {minimum:g}.")
    return number
