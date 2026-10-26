"""Alembic environment. The engine is supplied by the application (see sqlite.py)."""

import os
from pathlib import Path

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import create_engine

from claims_assistant.infrastructure.persistence.schema import metadata

config = context.config
target_metadata = metadata


def _cli_url() -> str:
    """The database the command-line alembic works on: the one the bot opens.

    DATABASE_PATH from the environment, then from .env, the same order as Settings.load;
    alembic.ini's url only when neither names one. Without this a server rollback
    (docs/operations.md) ran `alembic downgrade` against data/claims.sqlite3 under the app
    directory while the live database stayed at the newer revision.
    """
    raw = os.environ.get("DATABASE_PATH")
    if raw is None:
        raw = dotenv_values(".env", interpolate=False).get("DATABASE_PATH")
    if raw is None or not raw.strip():
        return config.get_main_option("sqlalchemy.url")
    return f"sqlite+pysqlite:///{Path(raw.strip()).as_posix()}"


def run_migrations_offline() -> None:
    context.configure(
        url=_cli_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The application passes its own connection; the CLI builds one for DATABASE_PATH.
    connection = config.attributes.get("connection")
    if connection is not None:
        _migrate(connection)
        return
    engine = create_engine(_cli_url())
    try:
        with engine.connect() as cli_connection:
            _migrate(cli_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
