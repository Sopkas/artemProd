"""The command-line alembic and a database newer than the code (review g8).

A rollback (docs/operations.md) runs `alembic downgrade` from the app directory. It must
work on the database the bot opens — DATABASE_PATH — not on alembic.ini's data/claims.sqlite3;
and a rolled-back bot that meets a newer schema must say so instead of `CommandError`.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.infrastructure.persistence import sqlite
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.runtime import app

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "123456789:synthetic_token_for_offline_tests_only"


def _revision(path: Path) -> str:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def _set_revision(path: Path, revision: str) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": revision})
    finally:
        engine.dispose()


def _live_database(directory: Path) -> Path:
    path = directory / "live" / "claims.sqlite3"
    open_sqlite_repository(path).close()  # head, as the bot leaves it
    return path


def _ini_outside_the_checkout(directory: Path) -> Path:
    """The shipped alembic.ini with its two cwd-relative paths made absolute.

    The command-line run below starts in tmp_path, not in the repository root: if env.py
    ever stopped honouring DATABASE_PATH, alembic would fall back to the ini's relative
    data/claims.sqlite3, and from the root that is the developer's working database — a
    regression (or a mutation run) would downgrade it instead of failing the test.
    """
    shipped = (ROOT / "alembic.ini").read_text(encoding="utf-8")
    patched = shipped.replace(
        "script_location = src/", f"script_location = {ROOT.as_posix()}/src/"
    ).replace("prepend_sys_path = src", f"prepend_sys_path = {(ROOT / 'src').as_posix()}")
    assert patched.count(ROOT.as_posix()) == 2, "alembic.ini paths changed; update this copy"
    path = directory / "alembic.ini"
    path.write_text(patched, encoding="utf-8")
    return path


def test_the_documented_downgrade_reaches_database_path(tmp_path):
    live = _live_database(tmp_path)
    head = _revision(live)
    ini = _ini_outside_the_checkout(tmp_path)
    # The documented command with the shipped settings, run away from any real database.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "downgrade", "-1"],
        cwd=tmp_path,
        env={**os.environ, "DATABASE_PATH": str(live)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert _revision(live) != head
    assert not (tmp_path / "data").exists()  # alembic.ini's own file was not touched


def test_the_cli_takes_database_path_from_env_file_when_the_environment_has_none(
    tmp_path, monkeypatch
):
    live = _live_database(tmp_path)
    head = _revision(live)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"DATABASE_PATH={live}\n", encoding="utf-8")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", sqlite._MIGRATIONS.as_posix())
    command.downgrade(config, "-1")
    assert _revision(live) != head
    assert not (tmp_path / "data").exists()  # alembic.ini's own file was not created


def test_a_database_newer_than_the_code_is_a_repository_error(tmp_path):
    live = _live_database(tmp_path)
    _set_revision(live, "9999")
    with pytest.raises(RepositoryError, match="новее этой версии"):
        open_sqlite_repository(live)


def test_the_start_check_names_a_newer_database(tmp_path, monkeypatch, capsys):
    live = _live_database(tmp_path)
    _set_revision(live, "9999")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "42")
    monkeypatch.setenv("DATABASE_PATH", str(live))
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "uploads"))
    monkeypatch.setenv("BACKUP_PATH", str(tmp_path / "backups"))
    assert app.main(["--check"]) == 1
    assert "Схема базы данных новее" in capsys.readouterr().err


async def test_downgrade_refuses_pending_purge(tmp_path, monkeypatch):
    from datetime import date

    from claims_assistant.domain.external import DataMode

    db = tmp_path / "claims.sqlite3"
    repository = open_sqlite_repository(db)
    run = await repository.create_run(42, date(2026, 10, 1), DataMode.DEMO)
    with repository.engine.begin() as connection:
        connection.execute(
            text("UPDATE analysis_runs SET status='purging' WHERE id=:id"), {"id": run.id}
        )
    repository.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", sqlite._MIGRATIONS.as_posix())
    with pytest.raises(RuntimeError, match="Завершите очистку"):
        command.downgrade(config, "0008")
    assert _revision(db) == "0009"
