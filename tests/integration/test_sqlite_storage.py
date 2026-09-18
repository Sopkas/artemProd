"""SQLite-specific behaviour: survives a restart, migrates in place, fails loudly."""

from datetime import date

import pytest

from claims_assistant.application.analysis_repository import NewFile, RepositoryError, RunNotFound
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository

OWNER = 42
DAY = date(2026, 9, 18)


async def test_runs_and_files_survive_reopening_the_database(tmp_path):
    path = tmp_path / "claims.sqlite3"
    first = open_sqlite_repository(path)
    run = await first.create_run(OWNER, DAY, DataMode.DEMO)
    stored = await first.add_file(
        OWNER,
        run.id,
        NewFile(FileKind.PAYMENTS, "b" * 64, 20, "r/payments.xlsx", Period(DAY, DAY)),
    )
    await first.transition(OWNER, run.id, RunStatus.QUEUED)
    first.close()

    second = open_sqlite_repository(path)
    reloaded = await second.get_run(OWNER, run.id)
    assert reloaded.status == RunStatus.QUEUED
    assert reloaded.files == (stored,)
    assert reloaded.created_at == run.created_at
    assert await second.list_runs(OWNER) == (reloaded,)
    second.close()


async def test_opening_twice_does_not_break_or_duplicate_schema(tmp_path):
    path = tmp_path / "claims.sqlite3"
    open_sqlite_repository(path).close()
    repository = open_sqlite_repository(path)
    run = await repository.create_run(OWNER, DAY, DataMode.LIVE)
    assert (await repository.get_run(OWNER, run.id)).mode == DataMode.LIVE
    repository.close()


async def test_missing_parent_directory_is_created(tmp_path):
    repository = open_sqlite_repository(tmp_path / "nested" / "dir" / "claims.sqlite3")
    await repository.create_run(OWNER, DAY, DataMode.DEMO)
    repository.close()


async def test_storage_failure_is_a_repository_error_not_an_empty_result(tmp_path):
    repository = open_sqlite_repository(tmp_path / "claims.sqlite3")
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    repository.close()
    with pytest.raises(RepositoryError):
        await repository.get_run(OWNER, run.id)
    with pytest.raises(RepositoryError):
        await repository.list_runs(OWNER)


async def test_owner_isolation_holds_across_reopen(tmp_path):
    path = tmp_path / "claims.sqlite3"
    first = open_sqlite_repository(path)
    run = await first.create_run(OWNER, DAY, DataMode.DEMO)
    first.close()
    second = open_sqlite_repository(path)
    with pytest.raises(RunNotFound):
        await second.get_run(99, run.id)
    second.close()
