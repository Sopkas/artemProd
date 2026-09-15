import pytest

from claims_assistant.runtime.settings import ConfigurationError, Settings

TOKEN = "123456789:synthetic_token_for_offline_tests_only"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_IDS", "LOG_LEVEL"):
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


def test_invalid_log_level(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("LOG_LEVEL", "secret")
    with pytest.raises(ConfigurationError, match="LOG_LEVEL") as error:
        Settings.load(tmp_path / "missing.env")
    assert "secret" not in str(error.value)
