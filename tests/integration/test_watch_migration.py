"""S8-09: migration 0006 adds the monitoring tables to a database that already has checks."""

from datetime import date

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.persistence import sqlite
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository

MONITORING_TABLES = {"watchlist", "watch_snapshots", "monitoring_days", "monitoring_outbox"}


def _migrate(path, target: str, *, down: bool = False) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    config = Config()
    config.set_main_option("script_location", sqlite._MIGRATIONS.as_posix())
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, target)
    engine.dispose()


def _tables(path) -> set[str]:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


async def test_an_existing_database_gains_the_tables_and_keeps_its_checks(tmp_path):
    path = tmp_path / "claims.sqlite3"
    _migrate(path, "0005")
    assert not MONITORING_TABLES & _tables(path)
    repository = open_sqlite_repository(path)  # 0005 → head, as the bot does at start
    try:
        run = await repository.create_run(42, date(2026, 10, 17), DataMode.DEMO)
    finally:
        repository.close()
    assert MONITORING_TABLES <= _tables(path)
    repository = open_sqlite_repository(path)
    try:
        assert [item.id for item in await repository.list_runs(42)] == [run.id]
    finally:
        repository.close()
    _migrate(path, "0005", down=True)
    tables = _tables(path)
    assert not MONITORING_TABLES & tables and "analysis_runs" in tables
