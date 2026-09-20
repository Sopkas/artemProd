from pathlib import Path

import pytest

from claims_assistant.infrastructure.demo.company_data import DemoScenario
from claims_assistant.runtime.settings import ConfigurationError, Settings

TOKEN = "123456789:synthetic_token_for_offline_tests_only"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "ALLOWED_TELEGRAM_IDS",
        "LOG_LEVEL",
        "DEMO_SCENARIO",
        "DATA_PROVIDER",
        "CHECKO_API_KEY",
        "DATABASE_PATH",
        "STORAGE_PATH",
        "EXTERNAL_TIMEOUT_SECONDS",
        "EXTERNAL_MAX_RETRIES",
        "EXTERNAL_CACHE_TTL_SECONDS",
        "RUN_TIME_LIMIT_SECONDS",
        "RUN_REQUEST_LIMIT",
        "BUSINESS_UTC_OFFSET_HOURS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_file_settings_and_safe_repr(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nALLOWED_TELEGRAM_IDS=42, 43,42\n", encoding="utf-8"
    )
    settings = Settings.load(env_file)
    assert settings.token == TOKEN
    assert settings.allowed_ids == frozenset({42, 43})
    assert settings.log_level == "INFO"
    assert TOKEN not in repr(settings)


def test_environment_overrides_file_including_empty_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nALLOWED_TELEGRAM_IDS=42\nLOG_LEVEL=INFO\n")
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "99")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings.load(env_file).allowed_ids == frozenset({99})
    assert Settings.load(env_file).log_level == "DEBUG"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        Settings.load(env_file)


@pytest.mark.parametrize("raw", ["", " ", "0", "-1", "42,", "42,,43", "x", "1.5", "４２", "9" * 16])
def test_rejects_invalid_allowlist(raw, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", raw)
    with pytest.raises(ConfigurationError, match="ALLOWED_TELEGRAM_IDS"):
        Settings.load(tmp_path / "missing.env")


@pytest.mark.parametrize("raw", ["", "invalid-secret"])
def test_rejects_token_without_disclosing_value(raw, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", raw)
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN") as error:
        Settings.load(tmp_path / "missing.env")
    if raw:
        assert raw not in str(error.value)


def test_database_path_defaults_to_data_dir_and_accepts_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    assert Settings.load(tmp_path / "missing.env").database_path == Path("data/claims.sqlite3")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "other.sqlite3"))
    assert Settings.load(tmp_path / "missing.env").database_path == tmp_path / "other.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", "   ")
    with pytest.raises(ConfigurationError, match="DATABASE_PATH"):
        Settings.load(tmp_path / "missing.env")


def test_storage_path_defaults_to_uploads_dir_and_accepts_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    assert Settings.load(tmp_path / "missing.env").storage_path == Path("data/uploads")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "files"))
    assert Settings.load(tmp_path / "missing.env").storage_path == tmp_path / "files"
    monkeypatch.setenv("STORAGE_PATH", " ")
    with pytest.raises(ConfigurationError, match="STORAGE_PATH"):
        Settings.load(tmp_path / "missing.env")


def test_demo_scenario_defaults_to_ordinary_and_accepts_any_case(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    assert Settings.load(tmp_path / "missing.env").demo_scenario == DemoScenario.ORDINARY
    monkeypatch.setenv("DEMO_SCENARIO", " Alarm ")
    assert Settings.load(tmp_path / "missing.env").demo_scenario == DemoScenario.ALARM


def test_invalid_demo_scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("DEMO_SCENARIO", "secret")
    with pytest.raises(ConfigurationError, match="DEMO_SCENARIO") as error:
        Settings.load(tmp_path / "missing.env")
    assert "secret" not in str(error.value)


def test_invalid_log_level(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("LOG_LEVEL", "secret")
    with pytest.raises(ConfigurationError, match="LOG_LEVEL") as error:
        Settings.load(tmp_path / "missing.env")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "provider,key,valid",
    [
        ("demo", "", True),
        ("checko", "synthetic-key", True),
        ("checko", "", False),
        ("other", "synthetic-key", False),
    ],
)
def test_provider_settings(monkeypatch, tmp_path, provider, key, valid):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("DATA_PROVIDER", provider)
    monkeypatch.setenv("CHECKO_API_KEY", key)
    if not valid:
        with pytest.raises(ConfigurationError):
            Settings.load(tmp_path / "missing.env")
    else:
        settings = Settings.load(tmp_path / "missing.env")
        assert settings.data_provider == provider
        assert settings.checko_api_key == key
        if key:
            assert key not in repr(settings)


def test_provider_environment_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nALLOWED_TELEGRAM_IDS=42\n"
        "DATA_PROVIDER=checko\nCHECKO_API_KEY=synthetic-key\n"
    )
    monkeypatch.setenv("DATA_PROVIDER", "demo")
    monkeypatch.setenv("CHECKO_API_KEY", "")
    assert Settings.load(path).data_provider == "demo"
    assert Settings.load(path).checko_api_key == ""


def test_external_limits_default_to_the_architecture_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    limits = Settings.load(tmp_path / "missing.env").external
    assert (limits.timeout_seconds, limits.max_retries) == (20.0, 2)
    assert limits.cache_ttl_seconds == 3600
    assert (limits.run_time_limit_seconds, limits.run_request_limit) == (900, 1500)


def test_external_limits_are_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("EXTERNAL_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("EXTERNAL_MAX_RETRIES", "0")
    monkeypatch.setenv("EXTERNAL_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("RUN_TIME_LIMIT_SECONDS", "60")
    monkeypatch.setenv("RUN_REQUEST_LIMIT", "10")
    limits = Settings.load(tmp_path / "missing.env").external
    assert limits.timeout_seconds == 5.5 and limits.max_retries == 0
    assert limits.cache_ttl_seconds == 0
    assert limits.run_time_limit_seconds == 60 and limits.run_request_limit == 10


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("EXTERNAL_TIMEOUT_SECONDS", "0"),
        ("EXTERNAL_TIMEOUT_SECONDS", "abc"),
        ("EXTERNAL_MAX_RETRIES", "-1"),
        ("EXTERNAL_MAX_RETRIES", "1.5"),
        ("EXTERNAL_CACHE_TTL_SECONDS", "-5"),
        ("RUN_TIME_LIMIT_SECONDS", "0"),
        ("RUN_REQUEST_LIMIT", "0"),
    ],
)
def test_invalid_external_limits_are_rejected_without_disclosing_value(
    tmp_path, monkeypatch, key, raw
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv(key, raw)
    with pytest.raises(ConfigurationError, match=key) as error:
        Settings.load(tmp_path / "missing.env")
    assert raw not in str(error.value) or raw in ("0",)


def test_business_timezone_defaults_to_moscow_and_accepts_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    assert Settings.load(tmp_path / "missing.env").business_utc_offset_hours == 3
    monkeypatch.setenv("BUSINESS_UTC_OFFSET_HOURS", "10")
    assert Settings.load(tmp_path / "missing.env").business_utc_offset_hours == 10
    monkeypatch.setenv("BUSINESS_UTC_OFFSET_HOURS", "15")
    with pytest.raises(ConfigurationError, match="BUSINESS_UTC_OFFSET_HOURS"):
        Settings.load(tmp_path / "missing.env")
    monkeypatch.setenv("BUSINESS_UTC_OFFSET_HOURS", "x")
    with pytest.raises(ConfigurationError, match="BUSINESS_UTC_OFFSET_HOURS"):
        Settings.load(tmp_path / "missing.env")


def test_retention_and_backup_defaults_and_overrides(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    settings = Settings.load(tmp_path / "missing.env")
    assert (settings.retention_days, settings.retention_sweep_seconds) == (30, 6 * 3600)
    assert (settings.backup_path, settings.backup_keep) == (Path("data/backups"), 7)
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("RETENTION_SWEEP_SECONDS", "600")
    monkeypatch.setenv("BACKUP_PATH", "var/backups")
    monkeypatch.setenv("BACKUP_KEEP", "2")
    settings = Settings.load(tmp_path / "missing.env")
    assert (settings.retention_days, settings.retention_sweep_seconds) == (7, 600)
    assert (settings.backup_path, settings.backup_keep) == (Path("var/backups"), 2)


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("RETENTION_DAYS", "0"),
        ("RETENTION_DAYS", "1.5"),
        ("RETENTION_SWEEP_SECONDS", "5"),
        ("BACKUP_KEEP", "0"),
    ],
)
def test_invalid_retention_settings_are_rejected(tmp_path, monkeypatch, key, raw):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv(key, raw)
    with pytest.raises(ConfigurationError, match=key):
        Settings.load(tmp_path / "missing.env")


@pytest.mark.parametrize(("raw", "expected"), [(None, "off"), ("stub", "stub"), ("STUB", "stub")])
def test_ai_provider_defaults_to_off(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    if raw is not None:
        monkeypatch.setenv("AI_PROVIDER", raw)
    assert Settings.load(tmp_path / "missing.env").ai_provider == expected


def test_unknown_ai_provider_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("AI_PROVIDER", "openai")
    with pytest.raises(ConfigurationError, match="AI_PROVIDER"):
        Settings.load(tmp_path / "missing.env")


def test_recommendation_provider_factory_follows_the_setting():
    from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
    from claims_assistant.runtime.ai import build_recommendation_provider

    assert build_recommendation_provider(Settings(TOKEN, frozenset({42}))) is None
    built = build_recommendation_provider(Settings(TOKEN, frozenset({42}), ai_provider="stub"))
    assert isinstance(built, StubRecommendationProvider)
