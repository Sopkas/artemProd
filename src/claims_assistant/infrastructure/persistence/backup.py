"""Backups of the SQLite database and the upload directory (S6-02).

A snapshot is a directory ``<backup root>/<UTC timestamp>/`` holding ``claims.sqlite3``
(taken with SQLite's online backup API, so it is consistent even while the bot is
running), ``uploads/`` (a copy of the storage directory) and ``manifest.json``. Older
snapshots beyond the kept count are removed. Restoring copies both back — with the bot
stopped — and the next start upgrades the schema if the release moved on.

Verification opens the copy: integrity check, row counts and whether every stored file
recorded in the database exists in the snapshot. That is what «проверить восстановление
на копии данных» means here.
"""

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DATABASE_NAME = "claims.sqlite3"
UPLOADS_NAME = "uploads"
MANIFEST_NAME = "manifest.json"
_STAMP = "%Y%m%dT%H%M%SZ"
# SQLite's own companions of a database file: a hot journal or WAL left by a crash would
# be replayed into whatever file sits at the database path — including a restored copy.
_SIDECARS = ("-journal", "-wal", "-shm")


class BackupError(RuntimeError):
    """A snapshot could not be written, read or restored; the message names no data."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    created_at: datetime
    runs: int
    files: int


@dataclass(frozen=True, slots=True)
class Verification:
    integrity: str  # "ok" or SQLite's first complaint
    runs: int
    files: int
    missing_files: int  # recorded uploads or reports absent from the snapshot

    @property
    def ok(self) -> bool:
        return self.integrity == "ok" and self.missing_files == 0


def _now() -> datetime:
    return datetime.now(UTC)


def _counts(database: Path) -> tuple[int, int]:
    # `closing`: a sqlite3 connection used as a context manager commits but stays open,
    # and an open handle would block moving or deleting the file on Windows.
    with closing(sqlite3.connect(database)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        runs = files = 0
        if "analysis_runs" in tables:
            runs = connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
        if "uploaded_files" in tables:
            files = connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0]
    return runs, files


def create_backup(
    database: Path,
    uploads: Path,
    root: Path,
    *,
    keep: int = 7,
    now: datetime | None = None,
) -> Snapshot:
    """Write a new snapshot and drop the oldest ones beyond ``keep``."""
    if keep < 1:
        raise BackupError("Нужно хранить хотя бы одну копию.")
    database, uploads, root = Path(database), Path(uploads), Path(root)
    if not database.exists():
        raise BackupError("Файл базы данных не найден; копировать нечего.")
    moment = now or _now()
    target = root / moment.astimezone(UTC).strftime(_STAMP)
    if target.exists():
        raise BackupError("Копия с этой отметкой времени уже существует.")
    try:
        target.mkdir(parents=True)
        with (
            closing(sqlite3.connect(database)) as source,
            closing(sqlite3.connect(target / DATABASE_NAME)) as copy,
        ):
            source.backup(copy)
        if uploads.exists():
            shutil.copytree(uploads, target / UPLOADS_NAME)
        else:
            (target / UPLOADS_NAME).mkdir()
        runs, files = _counts(target / DATABASE_NAME)
        manifest = {
            "created_at": moment.astimezone(UTC).isoformat(timespec="seconds"),
            "database": DATABASE_NAME,
            "uploads": UPLOADS_NAME,
            "runs": runs,
            "files": files,
        }
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except (OSError, sqlite3.Error) as error:
        shutil.rmtree(target, ignore_errors=True)
        raise BackupError("Не удалось записать копию.") from error
    _rotate(root, keep)
    return Snapshot(path=target, created_at=moment, runs=runs, files=files)


def list_snapshots(root: Path) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        path for path in root.iterdir() if path.is_dir() and (path / MANIFEST_NAME).exists()
    )


def _rotate(root: Path, keep: int) -> None:
    snapshots = list_snapshots(root)
    for old in snapshots[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def verify_backup(snapshot: Path) -> Verification:
    """Open the copy: integrity, counts, and every recorded file present."""
    snapshot = Path(snapshot)
    database = snapshot / DATABASE_NAME
    if not database.exists() or not (snapshot / MANIFEST_NAME).exists():
        raise BackupError("Каталог не похож на копию: нет базы или manifest.json.")
    try:
        with closing(sqlite3.connect(database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            paths: list[str] = []
            if "uploaded_files" in tables:
                paths += [
                    r[0] for r in connection.execute("SELECT stored_path FROM uploaded_files")
                ]
            if "report_artifacts" in tables:
                paths += [
                    r[0] for r in connection.execute("SELECT stored_path FROM report_artifacts")
                ]
        runs, files = _counts(database)
    except sqlite3.Error as error:
        raise BackupError("Копию базы не удалось прочитать.") from error
    uploads = snapshot / UPLOADS_NAME
    missing = sum(1 for path in paths if not (uploads / path).is_file())
    return Verification(integrity=integrity, runs=runs, files=files, missing_files=missing)


def restore_backup(
    snapshot: Path, database: Path, uploads: Path, *, now: datetime | None = None
) -> Verification:
    """Replace the live database and uploads with the snapshot's; verify first.

    The bot must be stopped: SQLite would otherwise keep writing to the replaced file.
    The current live data — the database with its journal/WAL companions and the
    uploads — is moved aside as ``<name>.before-restore-<UTC time>``; nothing is ever
    deleted here, so a second restore cannot destroy what the first one set aside.
    """
    snapshot, database, uploads = Path(snapshot), Path(database), Path(uploads)
    verification = verify_backup(snapshot)
    if verification.integrity != "ok":
        raise BackupError("Копия повреждена; восстановление отменено.")
    stamp = (now or _now()).astimezone(UTC).strftime(_STAMP)
    try:
        database.parent.mkdir(parents=True, exist_ok=True)
        _move_aside(database, stamp)
        for suffix in _SIDECARS:
            _move_aside(database.with_name(database.name + suffix), stamp)
        _move_aside(uploads, stamp)
        shutil.copy2(snapshot / DATABASE_NAME, database)
        shutil.copytree(snapshot / UPLOADS_NAME, uploads)
    except OSError as error:
        raise BackupError("Не удалось восстановить копию.") from error
    return verification


def _move_aside(path: Path, stamp: str) -> None:
    if not path.exists():
        return
    aside = path.with_name(f"{path.name}.before-restore-{stamp}")
    if aside.exists():
        raise BackupError("Отложенная копия с этой отметкой времени уже существует.")
    path.rename(aside)
