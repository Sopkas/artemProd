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
from claims_assistant.domain.export_1c import ExportHeader
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.inn import InvalidInn, validate_inn
from claims_assistant.domain.payments import SHEET_NAME as PAYMENTS_SHEET

from .analysis_repository import AnalysisRepository, NewFile
from .contract_link import merge_repeats
from .imports import (
    OutlineReader,
    RawSheetReader,
    SheetReader,
    WorkbookError,
    import_counterparties,
)
from .ledger_imports import (
    import_debt_history,
    import_interactions,
    import_payments,
    import_payments_export,
)


class StorageError(RuntimeError):
    """File storage failure or an unsafe path; never carries file contents."""


class PaymentsSheetWriter(Protocol):
    """Writes the «Платежи» sheet of the contract; the infrastructure supplies it.

    An export is stored translated (see ``accept_payments_export``), and the application
    layer may not reach for a workbook library of its own.
    """

    def build_payments_workbook(self, rows: list[list[object]]) -> bytes: ...


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
    # Set when the payments came as the customer's own 1C print (S4-05): what its header
    # said about the file, so the dialog can show whose export was accepted.
    export: ExportHeader | None = None


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
    *,
    uploaded: bytes | None = None,
) -> tuple[UploadedFile, bool]:
    """``uploaded`` is what the user sent when it is not what is stored (a translated
    export): the checksum names the upload, so the same file twice is one file."""
    stored_path = await asyncio.to_thread(files.save, run.id, data)
    new_file = NewFile(
        kind=kind,
        checksum=sha256(uploaded if uploaded is not None else data).hexdigest(),
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
    run: AnalysisRun,
    files: FileStorage,
    reader: SheetReader,
    limits: ImportLimits = ImportLimits(),
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
    # Counted the way the report will use them: one company per name in this file.
    counterparties, repeats = merge_repeats(result.counterparties)
    stored, duplicate = await _store(
        owner_id, run, FileKind.DEBT_REPORT, data, None, repository, files
    )
    run = await repository.get_run(owner_id, run.id)
    return LedgerAccepted(
        run=run,
        file=stored,
        rows=len(counterparties),
        issues=result.issues + repeats,
        duplicate=duplicate,
        contracts=sum(len(counterparty.contracts) for counterparty in counterparties),
    )


async def accept_payments_export(
    owner_id: int,
    run_id: str,
    data: bytes,
    *,
    inn: str,
    repository: AnalysisRepository,
    files: FileStorage,
    reader: RawSheetReader,
    sheets: PaymentsSheetWriter,
    limits: ImportLimits = ImportLimits(),
) -> LedgerAccepted | PackageRejected:
    """Attach the customer's own 1C print of one counterparty's payments (S4-05).

    The export says nothing about the INN — the dialog does, and it must be one of the
    «Контрагенты» file's own, or the payments would belong to nobody. The period comes
    from the export's header, so the user vouches for nothing they did not see.

    What is stored is the export translated into the «Платежи» sheet, not the print
    itself: everything downstream — the package review, the pipeline, the report — reads
    the package by the contract, and a check resumed a day later must not depend on the
    shape of the file it came from. The summary says so in as many words.
    """
    run = await repository.get_run(owner_id, run_id)
    known = await package_inns(run, files, reader, limits)
    try:
        # Both kinds: the customer's portfolio holds ООО and ИП alike (S7-02), and
        # payments are not a section that exists for companies only.
        inn = validate_inn(inn)
    except InvalidInn as error:
        return PackageRejected(issues=(_export_issue("export_inn_invalid", str(error)),))
    if inn not in known:
        return PackageRejected(
            issues=(
                _export_issue(
                    "export_inn_not_in_package",
                    "Этой организации нет в файле «Контрагенты» проверки.",
                ),
            )
        )
    result, export = await asyncio.to_thread(
        import_payments_export,
        reader,
        data,
        inn=inn,
        analysis_date=run.analysis_date,
        limits=limits,
    )
    if not result.rows:
        return PackageRejected(issues=result.issues)
    coverage = _up_to(export.period, run.analysis_date)
    sheet = await asyncio.to_thread(
        sheets.build_payments_workbook,
        [[row.inn, row.payment_id, row.paid_on, row.amount] for row in result.rows],
    )
    # The sheet is rebuilt on every upload and openpyxl stamps the time into it, so its
    # bytes differ each time: the export itself is what makes two uploads the same file.
    stored, duplicate = await _store(
        owner_id, run, FileKind.PAYMENTS, sheet, coverage, repository, files, uploaded=data
    )
    run = await repository.get_run(owner_id, run.id)
    return LedgerAccepted(
        run=run,
        file=stored,
        rows=len(result.rows),
        issues=result.issues,
        duplicate=duplicate,
        export=export,
    )


def _up_to(period: Period | None, day: date) -> Period | None:
    """The part of an export's period it can vouch for: payments after the analysis date
    are left out of the package, so the days after it are not covered either."""
    if period is None or period.start > day:
        return None
    return Period(period.start, min(period.end, day))


def _export_issue(code: str, reason: str) -> ImportIssue:
    return ImportIssue(code=code, severity=IssueSeverity.ERROR, sheet=PAYMENTS_SHEET, reason=reason)


async def launch_run(owner_id: int, run_id: str, repository: AnalysisRepository) -> AnalysisRun:
    """Freeze the package and queue the run; InvalidTransition if it is not a draft."""
    return await repository.transition(owner_id, run_id, RunStatus.QUEUED)


async def latest_run(owner_id: int, repository: AnalysisRepository) -> AnalysisRun | None:
    runs = await repository.list_runs(owner_id)
    return runs[0] if runs else None
