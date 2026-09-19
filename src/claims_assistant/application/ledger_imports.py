"""S4-05: import the optional «Платежи» and «История долга» sheets of a package.

Same boundary as ``imports.import_counterparties``: reader failures become one file-level
issue instead of an exception, so an unreadable upload yields no rows. The INNs of the
«Контрагенты» file are passed in: these sheets never add counterparties of their own.
"""

from collections.abc import Collection
from datetime import date

from claims_assistant.domain import debt_history, payments
from claims_assistant.domain.counterparties import ImportLimits
from claims_assistant.domain.debt_history import DebtHistoryImport
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.payments import PaymentsImport

from .imports import Sheet, SheetReader, WorkbookError


def _read(
    reader: SheetReader, source: object, sheet: str, limits: ImportLimits
) -> Sheet | ImportIssue:
    try:
        return reader.read(source, sheet, limits)
    except WorkbookError as error:
        return ImportIssue(
            code=error.code, severity=IssueSeverity.ERROR, sheet=sheet, reason=error.reason
        )


def import_payments(
    reader: SheetReader,
    source: object,
    *,
    known_inns: Collection[str] | None,
    analysis_date: date | None,
    limits: ImportLimits = ImportLimits(),
) -> PaymentsImport:
    sheet = _read(reader, source, payments.SHEET_NAME, limits)
    if isinstance(sheet, ImportIssue):
        return PaymentsImport(issues=(sheet,))
    header, rows = sheet
    return payments.parse_payments(
        header, rows, known_inns=known_inns, analysis_date=analysis_date, limits=limits
    )


def import_debt_history(
    reader: SheetReader,
    source: object,
    *,
    known_inns: Collection[str] | None,
    analysis_date: date | None,
    limits: ImportLimits = ImportLimits(),
) -> DebtHistoryImport:
    sheet = _read(reader, source, debt_history.SHEET_NAME, limits)
    if isinstance(sheet, ImportIssue):
        return DebtHistoryImport(issues=(sheet,))
    header, rows = sheet
    return debt_history.parse_debt_history(
        header, rows, known_inns=known_inns, analysis_date=analysis_date, limits=limits
    )
