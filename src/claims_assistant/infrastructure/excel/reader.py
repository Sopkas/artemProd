"""S2-04: openpyxl-backed sheet reader with archive-size and row guards.

The unpacked size is checked before openpyxl parses the archive, so a decompression
bomb is rejected without allocating its expanded content.
"""

import io
import zipfile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from claims_assistant.application.imports import (
    CorruptWorkbook,
    Sheet,
    SheetMissing,
    WorkbookTooLarge,
)
from claims_assistant.domain.counterparties import Cell, FormulaCell, ImportLimits
from claims_assistant.domain.export_1c import MAX_HEADER_ROWS as MAX_EXPORT_HEADER_ROWS


class OpenpyxlSheetReader:
    """Read a named sheet from an .xlsx given as bytes, a path or a binary stream."""

    def read(self, source: object, sheet: str, limits: ImportLimits) -> Sheet:
        data = self._as_bytes(source)
        self._guard_unpacked_size(data, limits)
        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError):
            raise CorruptWorkbook() from None
        try:
            if sheet not in workbook.sheetnames:
                raise SheetMissing(sheet)
            # Materialize rows before closing: read-only rows are tied to the open workbook.
            return self._extract(workbook[sheet], limits)
        finally:
            workbook.close()

    @staticmethod
    def _as_bytes(source: object) -> bytes:
        if isinstance(source, bytes):
            return source
        if isinstance(source, bytearray):
            return bytes(source)
        if isinstance(source, str):
            with open(source, "rb") as handle:
                return handle.read()
        if hasattr(source, "read"):
            return source.read()
        raise CorruptWorkbook()

    @staticmethod
    def _guard_unpacked_size(data: bytes, limits: ImportLimits) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                total = sum(info.file_size for info in archive.infolist())
        except zipfile.BadZipFile:
            raise CorruptWorkbook() from None
        if total > limits.max_unpacked_bytes:
            raise WorkbookTooLarge()

    def read_rows(
        self, source: object, limits: ImportLimits, sheet: str | None = None
    ) -> tuple[tuple[Cell, ...], ...]:
        """Every row of one sheet, header block included (S4-05).

        The customer's own export prints a parameters block above the table and names the
        sheet «Лист_1», so neither «the first row is the header» nor a known sheet name
        holds; ``sheet=None`` takes the workbook's first sheet.
        """
        data = self._as_bytes(source)
        self._guard_unpacked_size(data, limits)
        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError):
            raise CorruptWorkbook() from None
        try:
            if sheet is not None and sheet not in workbook.sheetnames:
                raise SheetMissing(sheet)
            worksheet = workbook[sheet] if sheet is not None else workbook[workbook.sheetnames[0]]
            rows = []
            for row in worksheet.iter_rows():
                rows.append(tuple(OpenpyxlSheetReader._cell(cell) for cell in row))
                if len(rows) > limits.max_rows + MAX_EXPORT_HEADER_ROWS:
                    break
            return tuple(rows)
        finally:
            workbook.close()

    @staticmethod
    def _extract(worksheet: object, limits: ImportLimits) -> Sheet:
        header: tuple[Cell, ...] = ()
        rows: list[tuple[int, tuple[Cell, ...]]] = []
        for offset, row in enumerate(worksheet.iter_rows()):
            cells = tuple(OpenpyxlSheetReader._cell(cell) for cell in row)
            if offset == 0:
                header = cells
                continue
            rows.append((offset + 1, cells))
            # One extra row past the limit lets the parser report the overflow.
            if len(rows) > limits.max_rows:
                break
        return header, rows

    @staticmethod
    def _cell(cell: object) -> Cell:
        value = getattr(cell, "value", None)
        # Detect formulas by data type and, defensively, by a leading "=".
        if getattr(cell, "data_type", None) == "f" or (
            isinstance(value, str) and value.startswith("=")
        ):
            return FormulaCell()
        return value
