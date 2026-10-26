"""SQLite-specific behaviour: survives a restart, migrates in place, fails loudly."""

import sqlite3
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event

from claims_assistant.application.analysis_repository import (
    InvalidTransition,
    NewFile,
    RepositoryError,
    RunNotFound,
)
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


# --- read-then-write under SQLite's write lock (review g6) ---


def _interloper(repository, path, trigger: str, sql: str, params=()):
    """Commit ``sql`` from a second connection just before ``trigger`` runs.

    pysqlite opens its transaction only at the first INSERT/UPDATE/DELETE, so a check
    read before that holds no lock and another writer can change the row in between.
    The second connection does not wait (timeout=0): if the repository holds the write
    lock, the interloper fails at once and records it.
    """
    outcome: list[str] = []

    @event.listens_for(repository.engine, "before_cursor_execute")
    def _race(_conn, _cursor, statement, *_args) -> None:
        if outcome or not statement.startswith(trigger):
            return
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute(sql, params)
            other.commit()
            outcome.append("committed")
        except sqlite3.OperationalError:
            outcome.append("locked")
        finally:
            other.close()

    return outcome


async def test_a_run_revived_during_its_purge_keeps_its_files(tmp_path):
    path = tmp_path / "claims.sqlite3"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    repository = open_sqlite_repository(path, clock=lambda: old)
    try:
        run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
        await repository.add_file(
            OWNER, run.id, NewFile(FileKind.PAYMENTS, "b" * 64, 20, "r/p.xlsx", Period(DAY, DAY))
        )
        revival = _interloper(
            repository,
            path,
            "DELETE FROM report_artifacts",
            "UPDATE analysis_runs SET status='queued', updated_at=? WHERE id=?",
            (datetime(2026, 9, 1, tzinfo=UTC).isoformat(), run.id),
        )
        deleted = await repository.delete_run(run.id, datetime(2026, 8, 1, tzinfo=UTC))
        # The revival has to wait for the purge: the run and its records go together.
        assert revival == ["locked"] and deleted is True
        with pytest.raises(RunNotFound):
            await repository.get_run(OWNER, run.id)
    finally:
        repository.close()


async def test_a_file_cannot_slip_into_a_package_launched_meanwhile(tmp_path):
    path = tmp_path / "claims.sqlite3"
    repository = open_sqlite_repository(path)
    try:
        run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
        launch = _interloper(
            repository,
            path,
            "INSERT INTO uploaded_files",
            "UPDATE analysis_runs SET status='queued' WHERE id=?",
            (run.id,),
        )
        await repository.add_file(
            OWNER, run.id, NewFile(FileKind.PAYMENTS, "b" * 64, 20, "r/p.xlsx", Period(DAY, DAY))
        )
        assert launch == ["locked"]
        assert (await repository.get_run(OWNER, run.id)).status == RunStatus.DRAFT
    finally:
        repository.close()


async def test_the_same_file_sent_twice_at_once_is_a_duplicate_not_a_failure(tmp_path):
    path = tmp_path / "claims.sqlite3"
    repository = open_sqlite_repository(path)
    try:
        run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
        twin = _interloper(
            repository,
            path,
            "INSERT INTO uploaded_files",
            "INSERT INTO uploaded_files (id, run_id, kind, checksum, size_bytes, stored_path,"
            " uploaded_at, sequence) VALUES ('twin', ?, 'payments', ?, 20, 'r/t.xlsx', ?, 1)",
            (run.id, "b" * 64, datetime(2026, 9, 1, tzinfo=UTC).isoformat()),
        )
        stored = await repository.add_file(
            OWNER, run.id, NewFile(FileKind.PAYMENTS, "b" * 64, 20, "r/p.xlsx", Period(DAY, DAY))
        )
        assert twin == ["locked"]
        assert (await repository.get_run(OWNER, run.id)).files == (stored,)
    finally:
        repository.close()


async def test_a_launch_that_lost_the_race_is_refused_not_reported_as_done(tmp_path):
    path = tmp_path / "claims.sqlite3"
    repository = open_sqlite_repository(path)
    try:
        run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
        other = _interloper(
            repository,
            path,
            "UPDATE analysis_runs",
            "UPDATE analysis_runs SET status='queued' WHERE id=?",
            (run.id,),
        )
        launched = await repository.transition(OWNER, run.id, RunStatus.QUEUED)
        assert other == ["locked"] and launched.status == RunStatus.QUEUED
        # The second launch, now strictly after the first, is refused as the contract says.
        with pytest.raises(InvalidTransition):
            await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    finally:
        repository.close()
