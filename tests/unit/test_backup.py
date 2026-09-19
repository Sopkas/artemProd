"""S6-02: backup, verification and restore of the database and uploads."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from claims_assistant.application.analysis_queue import RunOutcome
from claims_assistant.application.check_package import accept_counterparties
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.persistence.backup import (
    DATABASE_NAME,
    MANIFEST_NAME,
    UPLOADS_NAME,
    BackupError,
    create_backup,
    list_snapshots,
    restore_backup,
    verify_backup,
)
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.ops import run as ops_run
from claims_assistant.runtime.settings import Settings

OWNER = 42
DAY = date(2026, 9, 1)
T0 = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


async def populate(tmp_path: Path, finished: bool = True) -> tuple[Path, Path, str]:
    database = tmp_path / "data" / "claims.sqlite3"
    uploads = tmp_path / "data" / "uploads"
    repository = open_sqlite_repository(database)
    files = LocalFileStorage(uploads)
    try:
        result = await accept_counterparties(
            OWNER,
            DAY,
            DataMode.DEMO,
            build_counterparties_template(),
            repository=repository,
            files=files,
            reader=OpenpyxlSheetReader(),
        )
        run_id = result.run.id
        if finished:
            await repository.transition(OWNER, run_id, RunStatus.QUEUED)
            await repository.claim_next()
            await repository.finish(run_id, RunOutcome(RunStatus.COMPLETED))
            await repository.save_report(run_id, files.save(run_id, b"report-bytes"))
    finally:
        repository.close()
    return database, uploads, run_id


async def test_backup_is_consistent_verifiable_and_rotated(tmp_path):
    database, uploads, run_id = await populate(tmp_path)
    root = tmp_path / "backups"
    first = create_backup(database, uploads, root, keep=2, now=T0)
    assert first.path == root / "20260919T100000Z" and (first.runs, first.files) == (1, 1)
    assert (first.path / DATABASE_NAME).exists() and (first.path / MANIFEST_NAME).exists()
    assert (first.path / UPLOADS_NAME / run_id).is_dir()
    check = verify_backup(first.path)
    assert check.ok and (check.runs, check.files, check.missing_files) == (1, 1, 0)

    create_backup(database, uploads, root, keep=2, now=T0 + timedelta(hours=1))
    create_backup(database, uploads, root, keep=2, now=T0 + timedelta(hours=2))
    assert [p.name for p in list_snapshots(root)] == ["20260919T110000Z", "20260919T120000Z"]


async def test_backup_while_the_repository_is_open_and_writing(tmp_path):
    """The online backup API copies a database another connection holds open."""
    database, uploads, run_id = await populate(tmp_path, finished=False)
    repository = open_sqlite_repository(database)
    try:
        await repository.transition(OWNER, run_id, RunStatus.QUEUED)
        snapshot = create_backup(database, uploads, tmp_path / "backups", now=T0)
        await repository.claim_next()  # keeps writing after the snapshot
    finally:
        repository.close()
    from contextlib import closing

    with closing(sqlite3.connect(snapshot.path / DATABASE_NAME)) as copy:
        status = copy.execute("SELECT status FROM analysis_runs").fetchone()[0]
    assert status == "queued"  # the copy is a consistent point in time


async def test_verify_reports_missing_files_and_corruption(tmp_path):
    database, uploads, run_id = await populate(tmp_path)
    snapshot = create_backup(database, uploads, tmp_path / "backups", now=T0)
    for file in (snapshot.path / UPLOADS_NAME / run_id).iterdir():
        file.unlink()
    check = verify_backup(snapshot.path)
    assert check.integrity == "ok" and check.missing_files == 2 and not check.ok
    (snapshot.path / DATABASE_NAME).write_bytes(b"not a database")
    with pytest.raises(BackupError):
        verify_backup(snapshot.path)
    with pytest.raises(BackupError):
        verify_backup(tmp_path / "nowhere")


async def test_restore_brings_back_runs_and_files_after_a_loss(tmp_path):
    database, uploads, run_id = await populate(tmp_path)
    snapshot = create_backup(database, uploads, tmp_path / "backups", now=T0)
    # The loss: the database is wiped and a file is gone.
    database.unlink()
    for file in (uploads / run_id).iterdir():
        file.unlink()
    (uploads / "junk").mkdir()

    check = restore_backup(snapshot.path, database, uploads, now=T0)
    assert check.ok
    assert (uploads.with_name("uploads.before-restore-20260919T100000Z") / "junk").exists()
    repository = open_sqlite_repository(database)  # migrations run and find the schema
    try:
        run = await repository.get_run(OWNER, run_id)
        assert run.status == RunStatus.COMPLETED and len(run.files) == 1
        report = await repository.get_report(OWNER, run_id)
        assert LocalFileStorage(uploads).read(report.stored_path) == b"report-bytes"
        assert LocalFileStorage(uploads).read(run.files[0].stored_path)
    finally:
        repository.close()


async def test_restore_refuses_a_corrupt_snapshot_and_keeps_live_data(tmp_path):
    database, uploads, run_id = await populate(tmp_path)
    snapshot = create_backup(database, uploads, tmp_path / "backups", now=T0)
    (snapshot.path / DATABASE_NAME).write_bytes(b"garbage")
    with pytest.raises(BackupError):
        restore_backup(snapshot.path, database, uploads)
    assert database.exists() and (uploads / run_id).exists()


def settings_for(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        token="123456789:synthetic_token_for_offline_tests_only",
        allowed_ids=frozenset({OWNER}),
        database_path=tmp_path / "data" / "claims.sqlite3",
        storage_path=tmp_path / "data" / "uploads",
        backup_path=tmp_path / "backups",
        **overrides,
    )


def test_ops_commands_round_trip(tmp_path):
    """Synchronous on purpose: the purge command drives its own event loop."""
    import asyncio
    import io

    database, uploads, run_id = asyncio.run(populate(tmp_path))
    settings = settings_for(tmp_path, backup_keep=3, retention_days=1)
    out = io.StringIO()
    assert ops_run(["backup"], settings, out) == 0
    assert "Копия записана" in out.getvalue()
    snapshot = list_snapshots(settings.backup_path)[0]
    out = io.StringIO()
    assert ops_run(["list"], settings, out) == 0 and str(snapshot) in out.getvalue()
    out = io.StringIO()
    assert ops_run(["verify", str(snapshot)], settings, out) == 0
    assert "Целостность: ok" in out.getvalue()
    out = io.StringIO()
    assert ops_run(["restore", str(snapshot)], settings, out) == 2  # needs --yes
    out = io.StringIO()
    assert ops_run(["restore", str(snapshot), "--yes"], settings, out) == 0
    assert "Восстановлено" in out.getvalue()
    # Purge: the run is fresh, so nothing expires; dry-run says so without deleting.
    out = io.StringIO()
    assert ops_run(["purge", "--dry-run"], settings, out) == 0
    assert "истекло 0" in out.getvalue()
    out = io.StringIO()
    assert ops_run(["backup"], settings_for(tmp_path / "empty"), out) == 1
    assert "Ошибка" in out.getvalue()


async def test_restore_moves_a_hot_journal_aside_so_it_is_not_replayed(tmp_path):
    """Review B on #33: a journal left by a crash must not be applied to the restored copy."""
    database, uploads, run_id = await populate(tmp_path)
    snapshot = create_backup(database, uploads, tmp_path / "backups", now=T0)
    journal = database.with_name(database.name + "-journal")
    journal.write_bytes(bytes(range(256)) * 16)  # garbage, not a valid journal
    wal = database.with_name(database.name + "-wal")
    wal.write_bytes(b"garbage")
    check = restore_backup(snapshot.path, database, uploads, now=T0)
    assert check.ok
    assert not journal.exists() and not wal.exists()
    assert journal.with_name(journal.name + ".before-restore-20260919T100000Z").exists()
    assert verify_backup(snapshot.path).integrity == "ok"
    repository = open_sqlite_repository(database)
    try:
        assert (await repository.get_run(OWNER, run_id)).status == RunStatus.COMPLETED
    finally:
        repository.close()
    from contextlib import closing

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


async def test_second_restore_keeps_what_the_first_set_aside(tmp_path):
    """Review B on #33: nothing set aside is ever deleted by a later restore."""
    database, uploads, run_id = await populate(tmp_path)
    snapshot = create_backup(database, uploads, tmp_path / "backups", now=T0)
    restore_backup(snapshot.path, database, uploads, now=T0)
    restore_backup(snapshot.path, database, uploads, now=T0 + timedelta(minutes=1))
    aside = sorted(p.name for p in database.parent.iterdir() if "before-restore" in p.name)
    assert aside == [
        "claims.sqlite3.before-restore-20260919T100000Z",
        "claims.sqlite3.before-restore-20260919T100100Z",
        "uploads.before-restore-20260919T100000Z",
        "uploads.before-restore-20260919T100100Z",
    ]
    with pytest.raises(BackupError):
        restore_backup(snapshot.path, database, uploads, now=T0)  # same stamp: refuse
