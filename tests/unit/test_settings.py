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
