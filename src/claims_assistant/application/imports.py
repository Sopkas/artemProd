"""Application boundary for importing an uploaded package (S2-04).

The reader (infrastructure) turns a workbook into plain cells; the domain applies the
data contract. Reader failures are surfaced as a file-level import error, never raised
to the caller, so an unreadable upload simply yields no usable rows.
"""

from collections.abc import Iterable
from datetime import date
from typing import Protocol

from claims_assistant.domain.counterparties import (
    SHEET_NAME,
    Cell,
    CellRef,
    CounterpartiesImport,
    ImportLimits,
    RowError,
    parse_counterparties,
)

# (header cells, iterable of (excel row number, row cells)).
Sheet = tuple[tuple[Cell, ...], Iterable[tuple[int, tuple[Cell, ...]]]]


class WorkbookError(Exception):
    """Reader failure with a user-safe reason; never carries file contents."""

    reason = "Файл не удалось прочитать."


class CorruptWorkbook(WorkbookError):
    reason = "Файл не является корректным .xlsx."


class WorkbookTooLarge(WorkbookError):
    reason = "Распакованный файл превышает допустимый размер."


class SheetMissing(WorkbookError):
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
    """Read and validate the «Контрагенты» sheet, mapping reader failures to an error."""
    try:
        header, rows = reader.read(source, SHEET_NAME, limits)
    except WorkbookError as error:
        return CounterpartiesImport(errors=(RowError(error.reason, CellRef(SHEET_NAME)),))
    return parse_counterparties(header, rows, limits, analysis_date)
