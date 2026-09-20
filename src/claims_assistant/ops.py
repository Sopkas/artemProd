"""Operator commands (S6-02): backups, restore, verification and the retention purge.

    python -m claims_assistant.ops backup            # snapshot of the database and uploads
    python -m claims_assistant.ops list              # snapshots, newest last
    python -m claims_assistant.ops verify <dir>      # integrity, counts, missing files
    python -m claims_assistant.ops restore <dir>     # bot must be stopped; asks for --yes
    python -m claims_assistant.ops purge [--dry-run] # delete checks past the retention

Settings come from .env like the bot's own (DATABASE_PATH, STORAGE_PATH, BACKUP_PATH,
BACKUP_KEEP, RETENTION_DAYS). Output names counts and paths, never data.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claims_assistant.application.retention import purge_expired
from claims_assistant.infrastructure.persistence.backup import (
    BackupError,
    create_backup,
    list_snapshots,
    restore_backup,
    verify_backup,
)
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.runtime.settings import ConfigurationError, Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m claims_assistant.ops")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup", help="снять копию базы и загрузок")
    commands.add_parser("list", help="показать копии")
    verify = commands.add_parser("verify", help="проверить копию")
    verify.add_argument("snapshot", type=Path)
    restore = commands.add_parser("restore", help="восстановить из копии (бот остановлен)")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("--yes", action="store_true", help="подтвердить замену текущих данных")
    purge = commands.add_parser("purge", help="удалить проверки старше срока хранения")
    purge.add_argument("--dry-run", action="store_true", help="только посчитать")
    return parser


def run(argv: list[str], settings: Settings, out=sys.stdout) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            snapshot = create_backup(
                settings.database_path,
                settings.storage_path,
                settings.backup_path,
                keep=settings.backup_keep,
            )
            print(
                f"Копия записана: {snapshot.path} (проверок: {snapshot.runs}, "
                f"файлов: {snapshot.files}); хранится копий: {settings.backup_keep}.",
                file=out,
            )
        elif args.command == "list":
            snapshots = list_snapshots(settings.backup_path)
            if not snapshots:
                print(f"Копий нет в {settings.backup_path}.", file=out)
            for path in snapshots:
                print(path, file=out)
        elif args.command == "verify":
            result = verify_backup(args.snapshot)
            print(
                f"Целостность: {result.integrity}; проверок: {result.runs}; "
                f"файлов в базе: {result.files}; отсутствует файлов: {result.missing_files}.",
                file=out,
            )
            return 0 if result.ok else 2
        elif args.command == "restore":
            if not args.yes:
                print(
                    "Восстановление заменит текущую базу и загрузки. Остановите бота и "
                    "повторите команду с --yes.",
                    file=out,
                )
                return 2
            result = restore_backup(args.snapshot, settings.database_path, settings.storage_path)
            print(
                f"Восстановлено из {args.snapshot}: проверок {result.runs}, файлов {result.files}. "
                "Прежние данные сохранены рядом с суффиксом .before-restore; "
                "при следующем запуске бот применит миграции.",
                file=out,
            )
        elif args.command == "purge":
            repository = open_sqlite_repository(settings.database_path)
            try:
                summary = asyncio.run(
                    purge_expired(
                        repository,
                        LocalFileStorage(settings.storage_path),
                        retention=timedelta(days=settings.retention_days),
                        now=datetime.now(UTC),
                        dry_run=args.dry_run,
                    )
                )
            finally:
                repository.close()
            verb = "подлежит удалению" if args.dry_run else "удалено"
            print(
                f"Срок хранения {settings.retention_days} дн.: истекло {summary.expired}, "
                f"{verb} {summary.removed if not args.dry_run else summary.expired}, "
                f"не удалось {summary.failed}.",
                file=out,
            )
    except BackupError as error:
        print(f"Ошибка: {error}", file=out)
        return 1
    return 0


def main() -> int:
    try:
        settings = Settings.load()
    except ConfigurationError as error:
        print(f"Ошибка настроек: {error}", file=sys.stderr)
        return 1
    return run(sys.argv[1:], settings)


if __name__ == "__main__":
    raise SystemExit(main())
