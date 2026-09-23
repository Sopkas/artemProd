"""Package scenario (S2-03, S4-01): accept the package files, launch the run, show status.

The Telegram layer only hands over bytes and the owner; this module validates the sheet,
stores the file, records it on a draft run and applies the state machine. Blocking work
(parsing, disk) runs in a worker thread. The «Контрагенты» file opens the draft; the
optional «Платежи» (with the period the user vouches for) and «История долга» files are
checked against its INNs and attached to the same draft (S4-01).
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Protocol

from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus, UploadedFile
from claims_assistant.domain.counterparties import CounterpartyRow, ImportLimits
from claims_assistant.domain.debt_report import SHEET_NAME as DEBT_REPORT_SHEET
from claims_assistant.domain.debt_report import read_debt_report
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.domain.imports import ImportIssue, IssueSeverity

from .analysis_repository import AnalysisRepository, NewFile
from .imports import OutlineReader, SheetReader, WorkbookError, import_counterparties
from .ledger_imports import import_debt_history, import_interactions, import_payments


class StorageError(RuntimeError):
    """File storage failure or an unsafe path; never carries file contents."""


class FileStorage(Protocol):
    def save(self, run_id: str, data: bytes) -> str:
        """Write the bytes under the run's directory and return the relative path."""
        ...

    def remove(self, stored_path: str) -> None: ...

    def read(self, stored_path: str) -> bytes:
        """Return the stored bytes; StorageError if the file is missing or unsafe."""
        ...


@dataclass(frozen=True, slots=True)
class PackageAccepted:
    run: AnalysisRun
    file: UploadedFile
    rows: tuple[CounterpartyRow, ...]
    issues: tuple[ImportIssue, ...]
    duplicate: bool


@dataclass(frozen=True, slots=True)
class LedgerAccepted:
    """An optional file attached to the draft; rows are counted, never returned."""

    run: AnalysisRun
    file: UploadedFile
    rows: int
    issues: tuple[ImportIssue, ...]
    duplicate: bool
    # S7-01: how many contracts came with the overdue report's counterparties.
    contracts: int = 0


@dataclass(frozen=True, slots=True)
class PackageConflict:
    """The file cannot join this draft; the reason is safe to show."""

    reason: str


MAIN_FILE_ALREADY_IN_PACKAGE = (
    "Файл «Контрагенты» уже есть в пакете, а этот отличается. Чтобы заменить список, "
    "нажмите «Отмена» и начните новую проверку."
)


@dataclass(frozen=True, slots=True)
class PackageRejected:
    issues: tuple[ImportIssue, ...]


async def accept_counterparties(
    owner_id: int,
    analysis_date: date,
    mode: DataMode,
    data: bytes,
    *,
    repository: AnalysisRepository,
    files: FileStorage,
    reader: SheetReader,
    limits: ImportLimits = ImportLimits(),
    run_id: str | None = None,
) -> PackageAccepted | PackageRejected | PackageConflict:
    """Parse first: a file without usable rows never creates a run or touches the disk.

    Row-level errors are reported alongside the accepted rows; whether to launch with
    them is the user's decision, shown in the summary. A draft holds one «Контрагенты»
    file: the same file again is a duplicate, a different one is a conflict (S4-03) —
    the optional files were checked against the first list.
    """
    result = await asyncio.to_thread(import_counterparties, reader, data, limits, analysis_date)
    if not result.rows:
        return PackageRejected(issues=result.issues)

    if run_id is None:
        run = await repository.create_run(owner_id, analysis_date, mode)
    else:
        run = await repository.get_run(owner_id, run_id)
        checksum = sha256(data).hexdigest()
        for file in run.files:
            if file.kind is FileKind.COUNTERPARTIES and file.checksum != checksum:
                return PackageConflict(MAIN_FILE_ALREADY_IN_PACKAGE)

    stored, duplicate = await _store(
        owner_id, run, FileKind.COUNTERPARTIES, data, None, repository, files
    )
    run = await repository.get_run(owner_id, run.id)
    return PackageAccepted(
        run=run, file=stored, rows=result.rows, issues=result.issues, duplicate=duplicate
    )


LEDGER_KINDS = (FileKind.PAYMENTS, FileKind.DEBT_HISTORY, FileKind.INTERACTIONS)
_LEDGER_PARSERS = {
    FileKind.PAYMENTS: import_payments,
    FileKind.DEBT_HISTORY: import_debt_history,
    FileKind.INTERACTIONS: import_interactions,
}


async def _store(
    owner_id: int,
    run: AnalysisRun,
    kind: FileKind,
    data: bytes,
    coverage: Period | None,
    repository: AnalysisRepository,
    files: FileStorage,
) -> tuple[UploadedFile, bool]:
    stored_path = await asyncio.to_thread(files.save, run.id, data)
    new_file = NewFile(
        kind=kind,
        checksum=sha256(data).hexdigest(),
        size_bytes=len(data),
        stored_path=stored_path,
        coverage=coverage,
    )
    try:
        stored = await repository.add_file(owner_id, run.id, new_file)
    except Exception:
        await asyncio.to_thread(files.remove, stored_path)
        raise
    duplicate = stored.stored_path != stored_path
    if duplicate:
        # The same content is already in the package: keep the first copy only.
        await asyncio.to_thread(files.remove, stored_path)
    return stored, duplicate


async def package_inns(
    run: AnalysisRun, files: FileStorage, reader: SheetReader, limits: ImportLimits
) -> frozenset[str]:
    """INNs of the draft's «Контрагенты» file, re-read from storage (never from the chat)."""
    main = [file for file in run.files if file.kind is FileKind.COUNTERPARTIES]
    if not main:
        return frozenset()
    data = await asyncio.to_thread(files.read, main[0].stored_path)
    result = await asyncio.to_thread(import_counterparties, reader, data, limits, run.analysis_date)
    return frozenset(row.inn for row in result.rows)


async def accept_ledger(
    owner_id: int,
    run_id: str,
    kind: FileKind,
    data: bytes,
    *,
    coverage: Period | None,
    repository: AnalysisRepository,
    files: FileStorage,
    reader: SheetReader,
    limits: ImportLimits = ImportLimits(),
) -> LedgerAccepted | PackageRejected:
    """Attach «Платежи», «История долга» or «Взаимодействия» to the draft.

    The sheet is parsed against the package first (S4-05 and S4-02 importers).

    Rows of INNs outside the «Контрагенты» file are rejected by the parser; a file with no
    usable rows is not stored. Payments carry the period the user vouches for.
    """
    if kind not in LEDGER_KINDS:
        raise ValueError("Only the optional package files are attached this way")
    if kind is FileKind.PAYMENTS and coverage is None:
        raise ValueError("Payments need the covered period")
    run = await repository.get_run(owner_id, run_id)
    known = await package_inns(run, files, reader, limits)
    parse = _LEDGER_PARSERS[kind]
    result = await asyncio.to_thread(
        parse, reader, data, known_inns=known, analysis_date=run.analysis_date, limits=limits
    )
    if not result.rows:
        return PackageRejected(issues=result.issues)
    stored, duplicate = await _store(owner_id, run, kind, data, coverage, repository, files)
    run = await repository.get_run(owner_id, run.id)
    return LedgerAccepted(
        run=run, file=stored, rows=len(result.rows), issues=result.issues, duplicate=duplicate
    )


async def accept_debt_report(
    owner_id: int,
    run_id: str,
    data: bytes,
    *,
    repository: AnalysisRepository,
    files: FileStorage,
    reader: SheetReader,
    limits: ImportLimits = ImportLimits(),
) -> LedgerAccepted | PackageRejected:
    """Attach the customer's «Отчет по просроченным лизинговым платежам» (S7-01).

    It does not replace «Контрагенты» and is not checked against its INNs here: the report
    names no INN at all. Its counterparties are tied to the check by company name when the
    report is built, and what did not match is said there (decision of 23.09.2026).

    The file is read by the indents of its own rows, so the reader must be able to give
    them; one that cannot is a wiring mistake, not a bad file.
    """
    run = await repository.get_run(owner_id, run_id)
    if not isinstance(reader, OutlineReader):
        raise TypeError("A debt report needs a reader that returns row indents")
    try:
        rows = await asyncio.to_thread(reader.read_outline, data, limits)
    except WorkbookError as error:
        return PackageRejected(
            issues=(
                ImportIssue(
                    code=error.code,
                    severity=IssueSeverity.ERROR,
                    sheet=DEBT_REPORT_SHEET,
                    reason=error.reason,
                ),
            )
        )
    result = await asyncio.to_thread(read_debt_report, list(rows))
    if not result.counterparties:
        return PackageRejected(issues=result.issues)
    stored, duplicate = await _store(
        owner_id, run, FileKind.DEBT_REPORT, data, None, repository, files
    )
    run = await repository.get_run(owner_id, run.id)
    return LedgerAccepted(
        run=run,
        file=stored,
        rows=len(result.counterparties),
        issues=result.issues,
        duplicate=duplicate,
        contracts=result.contracts,
    )


async def launch_run(owner_id: int, run_id: str, repository: AnalysisRepository) -> AnalysisRun:
    """Freeze the package and queue the run; InvalidTransition if it is not a draft."""
    return await repository.transition(owner_id, run_id, RunStatus.QUEUED)


async def latest_run(owner_id: int, repository: AnalysisRepository) -> AnalysisRun | None:
    runs = await repository.list_runs(owner_id)
    return runs[0] if runs else None
