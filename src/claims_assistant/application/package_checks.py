"""Checks across the files of one package (S4-03).

Each sheet is validated on its own when it is uploaded (S2-04, S4-05, S4-02). This module
looks at the package as a whole, right before launch and again inside the pipeline:

- every file belongs to this run (its stored path lies in the run's own directory) — a
  file of another user or another check is never read;
- exactly one «Контрагенты» file; a second copy with other contents is refused;
- IDs repeated across several files of one kind: same data — a warning, other data — an
  error; debt snapshots for the same INN and date must agree between files;
- dates that contradict the main file: a debt snapshot on the analysis date that differs
  from the row's debt, a «last payment» in the main file that the covered payments
  export does not contain, or a later payment inside the covered period.

Cross-file issues carry ``file_id`` (the report will name the file by it, B's part);
reasons never quote comments, amounts or INNs. Date conflicts are also returned per INN
in ``PackageReview.conflicts`` so the indicators (S4-06) can mark exactly those companies
as «неизвестно» instead of the whole package.
"""

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import PurePosixPath
from typing import TypeVar

from claims_assistant.domain.analysis import AnalysisRun, FileKind, UploadedFile
from claims_assistant.domain.counterparties import CounterpartyRow, ImportLimits
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.debt_report import CounterpartyDebt, read_debt_report
from claims_assistant.domain.external import Period
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.indicators import (
    DEBT_HISTORY_CONFLICT,
    LAST_PAYMENT_CONFLICT,
    debt_contradicts,
    last_payment_contradicts,
)
from claims_assistant.domain.interactions import InteractionRow, chronology
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.plural import of_companies as _companies

from .check_package import FileStorage, StorageError
from .contract_link import merge_repeats, normalize_name
from .imports import OutlineReader, SheetReader, import_counterparties
from .ledger_imports import import_debt_history, import_interactions, import_payments

PACKAGE_SHEET = "Пакет"
FOREIGN_FILE = "Файл пакета не принадлежит этой проверке."
MAIN_FILE_DUPLICATED = "В пакете больше одного файла «Контрагенты»."

_Row = TypeVar("_Row")


class PackageIntegrityError(RuntimeError):
    """A file of the package is not this run's; the package must not be processed."""


@dataclass(frozen=True, slots=True)
class PackageReview:
    counterparties: tuple[CounterpartyRow, ...]
    payments: tuple[PaymentRow, ...]
    history: tuple[DebtSnapshot, ...]
    interactions: tuple[InteractionRow, ...]
    issues: tuple[ImportIssue, ...]  # every file's issues plus the cross-file ones
    # S7-01: the counterparties of the customer's overdue report with their contracts.
    # They carry no INN: linking to the rows above happens by name, later (contract_link).
    contracts: tuple[CounterpartyDebt, ...] = ()
    # INN → {DEBT_HISTORY_CONFLICT, LAST_PAYMENT_CONFLICT}: which indicator is unknown
    # for which company because the files contradict each other.
    conflicts: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # File id → what that file gave: usable rows, or contracts for an overdue report —
    # before rows repeated across files are merged, so each file is credited for its own.
    file_rows: Mapping[str, int] = field(default_factory=dict)

    @property
    def blocking(self) -> tuple[ImportIssue, ...]:
        """Package-level errors that make a launch pointless."""
        return tuple(
            issue
            for issue in self.issues
            if issue.sheet == PACKAGE_SHEET and issue.severity is IssueSeverity.ERROR
        )


def _package_issue(
    code: str, severity: IssueSeverity, reason: str, file_id: str | None = None
) -> ImportIssue:
    return ImportIssue(
        code=code, severity=severity, sheet=PACKAGE_SHEET, reason=reason, file_id=file_id
    )


def belongs_to_run(file: UploadedFile, run: AnalysisRun) -> bool:
    """The record names this run and its path is a plain file inside the run's directory."""
    parts = PurePosixPath(file.stored_path).parts
    return (
        file.run_id == run.id
        and len(parts) == 2
        and parts[0] == run.id
        and parts[1] not in ("", ".", "..")
    )


def check_ownership(run: AnalysisRun) -> None:
    """Every stored file must sit in this run's own directory; otherwise refuse to read."""
    for file in run.files:
        if not belongs_to_run(file, run):
            raise PackageIntegrityError(FOREIGN_FILE)


def _tag(issues: Iterable[ImportIssue], file_id: str) -> list[ImportIssue]:
    return [replace(issue, file_id=file_id) for issue in issues]


def _merge_by_key(
    groups: list[tuple[str, tuple[_Row, ...]]],
    key: Callable[[_Row], tuple],
    sheet: str,
    same_code: str,
    conflict_code: str,
    label: str,
) -> tuple[list[_Row], list[ImportIssue]]:
    """Rows of one kind from several files: a repeated key is a warning (same data) or an
    error (other data); the first file's row is kept."""
    accepted: dict[tuple, _Row] = {}
    issues: list[ImportIssue] = []
    for file_id, rows in groups:
        for row in rows:
            existing = accepted.get(key(row))
            if existing is None:
                accepted[key(row)] = row
            elif existing == row:
                issues.append(
                    ImportIssue(
                        code=same_code,
                        severity=IssueSeverity.WARNING,
                        sheet=sheet,
                        reason=f"{label} повторяется в другом файле пакета и учтён один раз.",
                        file_id=file_id,
                    )
                )
            else:
                issues.append(
                    ImportIssue(
                        code=conflict_code,
                        severity=IssueSeverity.ERROR,
                        sheet=sheet,
                        reason=f"{label} уже есть в другом файле пакета с другими данными.",
                        file_id=file_id,
                    )
                )
    return list(accepted.values()), issues


def date_conflicts(
    run: AnalysisRun,
    counterparties: tuple[CounterpartyRow, ...],
    payments: tuple[PaymentRow, ...],
    history: tuple[DebtSnapshot, ...],
    covered: dict[str, tuple[date, date]],
) -> tuple[list[ImportIssue], dict[str, frozenset[str]]]:
    """Contradictions between the main file and the optional ones, per INN.

    One issue per kind names how many companies are affected (never which); the mapping
    says which, so S4-06 marks the indicator unknown only for them.
    """
    # The rules themselves live in domain.indicators, so the indicators (S4-06) read the
    # same contradictions as unknown values.
    found: dict[str, set[str]] = {}
    periods = [Period(start, end) for start, end in covered.values()]
    paid: dict[str, list[date]] = {}
    for payment in payments:
        paid.setdefault(payment.inn, []).append(payment.paid_on)
    snapshots: dict[str, list[DebtSnapshot]] = {}
    for snapshot in history:
        snapshots.setdefault(snapshot.inn, []).append(snapshot)
    for row in {row.inn: row for row in counterparties}.values():
        # The main file's debt is as of the row's own cut-off date (contract §4).
        if debt_contradicts(row, snapshots.get(row.inn, ()), run.analysis_date):
            found.setdefault(row.inn, set()).add(DEBT_HISTORY_CONFLICT)
        if last_payment_contradicts(row.last_payment_date, paid.get(row.inn, ()), periods):
            found.setdefault(row.inn, set()).add(LAST_PAYMENT_CONFLICT)

    issues: list[ImportIssue] = []
    debt = sum(1 for kinds in found.values() if DEBT_HISTORY_CONFLICT in kinds)
    if debt:
        issues.append(
            _package_issue(
                "debt_history_conflict",
                IssueSeverity.WARNING,
                f"У {_companies(debt)} сумма долга в «Истории долга» на дату среза расходится "
                "с файлом «Контрагенты»; динамика долга по ним считается неизвестной "
                "до исправления.",
            )
        )
    payment = sum(1 for kinds in found.values() if LAST_PAYMENT_CONFLICT in kinds)
    if payment:
        issues.append(
            _package_issue(
                "last_payment_conflict",
                IssueSeverity.WARNING,
                f"У {_companies(payment)} дата последнего платежа в файле «Контрагенты» "
                "не совпадает с выгрузкой «Платежи» за подтверждённый период; давность "
                "платежа по ним считается неизвестной до исправления.",
            )
        )
    return issues, {inn: frozenset(kinds) for inn, kinds in found.items()}


async def review_package(
    run: AnalysisRun,
    files: FileStorage,
    reader: SheetReader,
    limits: ImportLimits = ImportLimits(),
) -> PackageReview:
    """Re-read every file of the package and check them against each other.

    PackageIntegrityError if a file is not this run's; StorageError if a file is gone.
    """
    check_ownership(run)
    main = [file for file in run.files if file.kind is FileKind.COUNTERPARTIES]
    issues: list[ImportIssue] = []
    if len(main) > 1:
        issues.append(
            _package_issue("counterparties_duplicated", IssueSeverity.ERROR, MAIN_FILE_DUPLICATED)
        )
    counterparties: tuple[CounterpartyRow, ...] = ()
    if main:
        data = await asyncio.to_thread(files.read, main[0].stored_path)
        result = await asyncio.to_thread(
            import_counterparties, reader, data, limits, run.analysis_date
        )
        counterparties = result.rows
        issues.extend(_tag(result.issues, main[0].id))
    known = frozenset(row.inn for row in counterparties)

    parsers = {
        FileKind.PAYMENTS: import_payments,
        FileKind.DEBT_HISTORY: import_debt_history,
        FileKind.INTERACTIONS: import_interactions,
    }
    parsed: dict[FileKind, list[tuple[str, tuple]]] = {kind: [] for kind in parsers}
    covered: dict[str, tuple[date, date]] = {}
    file_rows: dict[str, int] = {}
    for file in run.files:
        parse = parsers.get(file.kind)
        if parse is None:
            continue
        data = await asyncio.to_thread(files.read, file.stored_path)
        result = await asyncio.to_thread(
            parse, reader, data, known_inns=known, analysis_date=run.analysis_date, limits=limits
        )
        parsed[file.kind].append((file.id, result.rows))
        file_rows[file.id] = len(result.rows)
        issues.extend(_tag(result.issues, file.id))
        if file.kind is FileKind.PAYMENTS and file.coverage is not None:
            covered[file.id] = (file.coverage.start, file.coverage.end)

    contracts, contract_issues, contract_counts = await _read_debt_reports(
        run, files, reader, limits
    )
    issues.extend(contract_issues)
    file_rows.update(contract_counts)

    payments, more = _merge_by_key(
        parsed[FileKind.PAYMENTS],
        lambda p: (p.inn, p.payment_id),
        "Платежи",
        "duplicate_payment_across_files",
        "payment_id_conflict_across_files",
        "ID платежа",
    )
    issues.extend(more)
    history, more = _merge_by_key(
        parsed[FileKind.DEBT_HISTORY],
        lambda s: (s.inn, s.cutoff_date),
        "История долга",
        "duplicate_snapshot_across_files",
        "snapshot_conflict_across_files",
        "Срез долга",
    )
    issues.extend(more)
    interactions, more = _merge_by_key(
        parsed[FileKind.INTERACTIONS],
        lambda i: (i.inn, i.interaction_id),
        "Взаимодействия",
        "duplicate_interaction_across_files",
        "interaction_id_conflict_across_files",
        "ID взаимодействия",
    )
    issues.extend(more)
    conflict_issues, conflicts = date_conflicts(
        run, counterparties, tuple(payments), tuple(history), covered
    )
    issues.extend(conflict_issues)
    ordered_interactions = tuple(row for rows in chronology(interactions).values() for row in rows)
    return PackageReview(
        counterparties=counterparties,
        payments=tuple(payments),
        history=tuple(history),
        interactions=ordered_interactions,
        contracts=contracts,
        issues=tuple(issues),
        conflicts=conflicts,
        file_rows=file_rows,
    )


async def _read_debt_reports(
    run: AnalysisRun,
    files: FileStorage,
    reader: SheetReader,
    limits: ImportLimits,
) -> tuple[tuple[CounterpartyDebt, ...], list[ImportIssue], dict[str, int]]:
    """The customer's overdue reports of this package, counterparty by counterparty, and
    how many contracts each file gave.

    The file is read by the indents of its own rows, so the reader has to be able to give
    them; a reader that cannot is a wiring mistake, not a bad file.

    Inside one report, the entries of one name are one company (``merge_repeats``). A
    counterparty named in two reports keeps the contracts of the first one: two prints of
    the same client are two views of one debt, and adding them up would double it.
    """
    attached = [file for file in run.files if file.kind is FileKind.DEBT_REPORT]
    if not attached:
        return (), [], {}
    if not isinstance(reader, OutlineReader):
        raise TypeError("A debt report needs a reader that returns row indents")
    issues: list[ImportIssue] = []
    counterparties: list[CounterpartyDebt] = []
    counts: dict[str, int] = {}
    seen: dict[str, str] = {}  # normalised name → the file that brought it first
    for file in attached:
        data = await asyncio.to_thread(files.read, file.stored_path)
        rows = await asyncio.to_thread(reader.read_outline, data, limits)
        result = await asyncio.to_thread(read_debt_report, list(rows))
        merged, repeats = merge_repeats(result.counterparties)
        issues.extend(_tag(result.issues + repeats, file.id))
        counts[file.id] = sum(len(counterparty.contracts) for counterparty in merged)
        for counterparty in merged:
            key = normalize_name(counterparty.name)
            if key in seen:
                issues.append(
                    _package_issue(
                        "debt_report_duplicate_across_files",
                        IssueSeverity.WARNING,
                        f"Контрагент «{counterparty.name}» есть в двух отчётах по договорам; "
                        "договоры взяты из первого.",
                    )
                )
                continue
            seen[key] = file.id
            counterparties.append(counterparty)
    return tuple(counterparties), issues, counts


__all__ = [
    "DEBT_HISTORY_CONFLICT",
    "FOREIGN_FILE",
    "LAST_PAYMENT_CONFLICT",
    "MAIN_FILE_DUPLICATED",
    "PACKAGE_SHEET",
    "PackageIntegrityError",
    "PackageReview",
    "StorageError",
    "belongs_to_run",
    "check_ownership",
    "date_conflicts",
    "review_package",
]
