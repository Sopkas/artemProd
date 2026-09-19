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

Cross-file issues carry ``file_id`` so the report's «Качество данных» sheet names the
file; reasons never quote comments, amounts or INNs.
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from typing import TypeVar

from claims_assistant.domain.analysis import AnalysisRun, FileKind, UploadedFile
from claims_assistant.domain.counterparties import CounterpartyRow, ImportLimits
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.interactions import InteractionRow, chronology
from claims_assistant.domain.payments import PaymentRow

from .check_package import FileStorage, StorageError
from .imports import SheetReader, import_counterparties
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
    return file.run_id == run.id and file.stored_path.startswith(f"{run.id}/")


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
) -> list[ImportIssue]:
    """Contradictions between the main file and the optional ones; reasons carry no data."""
    issues: list[ImportIssue] = []
    by_inn = {row.inn: row for row in counterparties}
    for snapshot in history:
        row = by_inn.get(snapshot.inn)
        if row is None or snapshot.cutoff_date != run.analysis_date or row.debt is None:
            continue
        if snapshot.debt != row.debt:
            issues.append(
                _package_issue(
                    "debt_history_conflict",
                    IssueSeverity.WARNING,
                    "Сумма долга в «Истории долга» на дату анализа расходится с файлом "
                    "«Контрагенты»; показатель динамики долга по этой организации "
                    "не рассчитывается.",
                )
            )
            break
    paid: dict[str, list[date]] = {}
    for payment in payments:
        paid.setdefault(payment.inn, []).append(payment.paid_on)
    for inn, row in by_inn.items():
        last = row.last_payment_date
        if last is None or not covered:
            continue
        # Only the period the user vouched for can contradict the main file.
        inside = any(start <= last <= end for start, end in covered.values())
        dates = paid.get(inn, [])
        later = [d for d in dates if d > last and any(s <= d <= e for s, e in covered.values())]
        if (inside and last not in dates) or later:
            issues.append(
                _package_issue(
                    "last_payment_conflict",
                    IssueSeverity.WARNING,
                    "Дата последнего платежа в файле «Контрагенты» не совпадает с выгрузкой "
                    "«Платежи» за подтверждённый период; давность платежа берётся из выгрузки.",
                )
            )
            break
    return issues


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
    for file in run.files:
        parse = parsers.get(file.kind)
        if parse is None:
            continue
        data = await asyncio.to_thread(files.read, file.stored_path)
        result = await asyncio.to_thread(
            parse, reader, data, known_inns=known, analysis_date=run.analysis_date, limits=limits
        )
        parsed[file.kind].append((file.id, result.rows))
        issues.extend(_tag(result.issues, file.id))
        if file.kind is FileKind.PAYMENTS and file.coverage is not None:
            covered[file.id] = (file.coverage.start, file.coverage.end)

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
    issues.extend(date_conflicts(run, counterparties, tuple(payments), tuple(history), covered))
    ordered_interactions = tuple(row for rows in chronology(interactions).values() for row in rows)
    return PackageReview(
        counterparties=counterparties,
        payments=tuple(payments),
        history=tuple(history),
        interactions=ordered_interactions,
        issues=tuple(issues),
    )


__all__ = [
    "FOREIGN_FILE",
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
