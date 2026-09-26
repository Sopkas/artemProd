"""Application boundary for importing an uploaded package (S2-04).

The reader (infrastructure) turns a workbook into plain cells; the domain applies the
data contract. Reader failures are surfaced as a file-level import error, never raised
to the caller, so an unreadable upload simply yields no usable rows.
"""

from collections.abc import Iterable
from datetime import date
from typing import Protocol, runtime_checkable

from claims_assistant.domain.counterparties import (
    SHEET_NAME,
    Cell,
    CounterpartiesImport,
    ImportLimits,
    parse_counterparties,
)
from claims_assistant.domain.imports import ImportIssue, IssueSeverity

# (header cells, iterable of (excel row number, row cells)).
Sheet = tuple[tuple[Cell, ...], Iterable[tuple[int, tuple[Cell, ...]]]]


@runtime_checkable
class OutlineReader(Protocol):
    """Reads rows with the indent of the first cell (S7-01: the 1C report's hierarchy)."""

    def read_outline(
        self, source: object, limits: ImportLimits, sheet: str | None = None
    ) -> tuple[tuple[int, tuple[Cell, ...]], ...]: ...


class RawSheetReader(Protocol):
    """Reads a sheet as it is, header block and all (S4-05: the customer's own export)."""

    def read_rows(
        self, source: object, limits: ImportLimits, sheet: str | None = None
    ) -> tuple[tuple[Cell, ...], ...]: ...


class WorkbookError(Exception):
    """Reader failure with a stable code and user-safe reason; never carries contents."""

    code = "workbook_error"
    reason = "Файл не удалось прочитать."


class CorruptWorkbook(WorkbookError):
    code = "workbook_corrupt"
    reason = "Файл не является корректным .xlsx."


class WorkbookTooLarge(WorkbookError):
    code = "workbook_too_large"
    reason = "Распакованный файл превышает допустимый размер."


class SheetMissing(WorkbookError):
    code = "sheet_missing"

    def __init__(self, sheet: str) -> None:
        super().__init__(sheet)
        self.reason = f"Лист «{sheet}» не найден в файле."


class SheetReader(Protocol):
    def read(self, source: object, sheet: str, limits: ImportLimits) -> Sheet:
        """Return the header and rows of a named sheet.

        Expected failures (corrupt file, missing sheet, oversized archive) raise a
        WorkbookError. Programming errors and cancellation propagate.
        """
        ...


def import_counterparties(
    reader: SheetReader,
    source: object,
    limits: ImportLimits = ImportLimits(),
    analysis_date: date | None = None,
) -> CounterpartiesImport:
    """Read and validate the «Контрагенты» sheet, mapping reader failures to an issue."""
    try:
        header, rows = reader.read(source, SHEET_NAME, limits)
    except WorkbookError as error:
        return CounterpartiesImport(
            issues=(
                ImportIssue(
                    code=error.code,
                    severity=IssueSeverity.ERROR,
                    sheet=SHEET_NAME,
                    reason=error.reason,
                ),
            )
        )
    return parse_counterparties(header, rows, limits, analysis_date)
