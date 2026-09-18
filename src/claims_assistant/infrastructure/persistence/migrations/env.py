"""Alembic environment. The engine is supplied by the application (see sqlite.py)."""

from alembic import context
from sqlalchemy import create_engine

from claims_assistant.infrastructure.persistence.schema import metadata

config = context.config
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
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
    # The application passes its own connection; the CLI builds one from alembic.ini.
    connection = config.attributes.get("connection")
    if connection is not None:
        _migrate(connection)
        return
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as cli_connection:
            _migrate(cli_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
