"""S2-04: sheet reader with archive-size and row guards; .xls as well since S4-05.

The unpacked size is checked before openpyxl parses the archive, so a decompression
bomb is rejected without allocating its expanded content.

The customer's own exports come out of 1C in the old .xls format, so that is read too,
through ``xlrd``. One difference cannot be papered over: **a formula inside an .xls is
invisible to us**. The file keeps the value Excel computed last, and the library hands it
over as if it had been typed; in an .xlsx a formula is refused, because a saved value may
be stale. ``is_legacy_xls`` tells the formats apart, but no caller warns the user about
this today (docs/data-contracts.md says so too).

A sheet is read as its cells are stored, not as its header claims (review g6). A read-only
.xlsx otherwise trusts ``<dimension>``: an understated one drops rows and columns without
a word, an inflated one (``A1:XFD…``) pads every row to 16 384 cells, and a file of a few
kilobytes then takes more memory than the server has. An .xls is opened with ragged rows
for the same reason: one cell in the far corner made xlrd build the whole 65 536 × 256
grid. Neither format is read wider than ``MAX_COLUMNS``.
"""

import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time

import xlrd
from openpyxl import load_workbook

from claims_assistant.application.imports import (
    CorruptWorkbook,
    Sheet,
    SheetMissing,
    WorkbookError,
    WorkbookTooLarge,
)
from claims_assistant.domain.counterparties import Cell, FormulaCell, ImportLimits
from claims_assistant.domain.export_1c import MAX_HEADER_ROWS as MAX_EXPORT_HEADER_ROWS

# Compound File Binary header: every .xls — and any other OLE2 document — starts with it.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# The widest sheet that is read: the .xls format's own limit, column IV. Every contract
# finds its columns by their header, so a header further right is reported as missing
# rather than lost; without a bound a sheet's width is whatever its file claims.
MAX_COLUMNS = 256


def is_legacy_xls(source: bytes) -> bool:
    """Whether these bytes are an old-format .xls rather than an .xlsx archive."""
    return source[:8] == _OLE2_MAGIC


@contextmanager
def _broken_file_is_corrupt() -> Iterator[None]:
    """Whatever a sheet breaks with while it is opened or read is a broken file.

    Both libraries parse lazily: a read-only .xlsx parses each sheet only while its rows
    are iterated, so bad sheet XML or a bad number surfaces there as ParseError or
    ValueError, not at opening. The callers promise a file-level import error, never an
    exception (application/imports.py). ``from None``: the message may quote a cell.
    """
    try:
        yield
    except WorkbookError:
        raise
    except Exception:
        raise CorruptWorkbook() from None


def _rectangular(rows: list[tuple]) -> list[tuple]:
    """Pad stored-as-is rows to one width, the shape a sheet's dimension used to give."""
    width = max((len(row) for row in rows), default=0)
    return [row + (None,) * (width - len(row)) for row in rows]


def _xlsx_rows(worksheet: object) -> Iterator[tuple]:
    """The rows the sheet's XML holds, each cut at ``MAX_COLUMNS``; not padded."""
    worksheet.reset_dimensions()
    for row in worksheet.iter_rows():
        yield tuple(row[:MAX_COLUMNS])


class OpenpyxlSheetReader:
    """Read a named sheet from an .xlsx given as bytes, a path or a binary stream."""

    def read(self, source: object, sheet: str, limits: ImportLimits) -> Sheet:
        data = self._as_bytes(source)
        if is_legacy_xls(data):
            rows = self._legacy_rows(data, limits, sheet)
            header = rows[0] if rows else ()
            return header, [(index + 2, cells) for index, cells in enumerate(rows[1:])]
        with self._xlsx(data, limits) as workbook:
            if sheet not in workbook.sheetnames:
                raise SheetMissing(sheet)
            # Materialize rows before closing: read-only rows are tied to the open workbook.
            return self._extract(workbook[sheet], limits)

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

    @classmethod
    @contextmanager
    def _xlsx(cls, data: bytes, limits: ImportLimits) -> Iterator[object]:
        """The open read-only workbook; any failure inside is a CorruptWorkbook."""
        cls._guard_unpacked_size(data, limits)
        with _broken_file_is_corrupt():
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                yield workbook
            finally:
                workbook.close()

    @staticmethod
    @contextmanager
    def _xls(data: bytes, limits: ImportLimits, **options: bool) -> Iterator[xlrd.book.Book]:
        """The open .xls workbook, rows ragged; any failure inside is a CorruptWorkbook."""
        if len(data) > limits.max_unpacked_bytes:
            raise WorkbookTooLarge()
        # xlrd raises its own family for every kind of broken file, and more while reading.
        with _broken_file_is_corrupt():
            book = xlrd.open_workbook(file_contents=data, ragged_rows=True, **options)
            try:
                yield book
            finally:
                book.release_resources()

    @staticmethod
    def _guard_unpacked_size(data: bytes, limits: ImportLimits) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                total = sum(info.file_size for info in archive.infolist())
        except zipfile.BadZipFile:
            raise CorruptWorkbook() from None
        if total > limits.max_unpacked_bytes:
            raise WorkbookTooLarge()

    def read_outline(
        self, source: object, limits: ImportLimits, sheet: str | None = None
    ) -> tuple[tuple[int, tuple[Cell, ...]], ...]:
        """Rows with the indent of their first cell (S7-01).

        A 1C report puts its hierarchy in one column and shows the level by indenting:
        the sales point at the left, the counterparty deeper, the contract deeper still.
        The step differs between files (2/4 in one export, 3/6 in another), so callers
        must rank the values rather than compare them with a constant.
        """
        data = self._as_bytes(source)
        if is_legacy_xls(data):
            return self._legacy_outline(data, limits, sheet)
        with self._xlsx(data, limits) as workbook:
            if sheet is not None and sheet not in workbook.sheetnames:
                raise SheetMissing(sheet)
            worksheet = workbook[sheet] if sheet is not None else workbook[workbook.sheetnames[0]]
            indents: list[int] = []
            rows: list[tuple[Cell, ...]] = []
            for row in _xlsx_rows(worksheet):
                alignment = getattr(row[0], "alignment", None) if row else None
                indents.append(int(getattr(alignment, "indent", 0) or 0))
                rows.append(tuple(OpenpyxlSheetReader._cell(cell) for cell in row))
                if len(rows) > limits.max_rows + MAX_EXPORT_HEADER_ROWS:
                    break
            return tuple(zip(indents, _rectangular(rows), strict=True))

    @staticmethod
    def _legacy_outline(
        data: bytes, limits: ImportLimits, sheet: str | None
    ) -> tuple[tuple[int, tuple[Cell, ...]], ...]:
        # formatting_info: the indent lives in the cell format, not in the value.
        with OpenpyxlSheetReader._xls(data, limits, formatting_info=True) as book:
            worksheet = OpenpyxlSheetReader._legacy_sheet(book, sheet)
            indents: list[int] = []
            rows: list[tuple[Cell, ...]] = []
            for index in range(worksheet.nrows):
                indent = 0
                if worksheet.row_len(index):
                    fmt = book.xf_list[worksheet.cell_xf_index(index, 0)]
                    indent = int(getattr(fmt.alignment, "indent_level", 0) or 0)
                indents.append(indent)
                rows.append(OpenpyxlSheetReader._legacy_row(book, worksheet, index))
                if len(rows) > limits.max_rows + MAX_EXPORT_HEADER_ROWS:
                    break
            return tuple(zip(indents, _rectangular(rows), strict=True))

    def read_rows(
        self, source: object, limits: ImportLimits, sheet: str | None = None
    ) -> tuple[tuple[Cell, ...], ...]:
        """Every row of one sheet, header block included (S4-05).

        The customer's own export prints a parameters block above the table and names the
        sheet «Лист_1», so neither «the first row is the header» nor a known sheet name
        holds; ``sheet=None`` takes the workbook's first sheet.
        """
        data = self._as_bytes(source)
        if is_legacy_xls(data):
            return self._legacy_rows(data, limits, sheet)
        with self._xlsx(data, limits) as workbook:
            if sheet is not None and sheet not in workbook.sheetnames:
                raise SheetMissing(sheet)
            worksheet = workbook[sheet] if sheet is not None else workbook[workbook.sheetnames[0]]
            rows = []
            for row in _xlsx_rows(worksheet):
                rows.append(tuple(OpenpyxlSheetReader._cell(cell) for cell in row))
                if len(rows) > limits.max_rows + MAX_EXPORT_HEADER_ROWS:
                    break
            return tuple(_rectangular(rows))

    @staticmethod
    def _extract(worksheet: object, limits: ImportLimits) -> Sheet:
        header: tuple[Cell, ...] = ()
        rows: list[tuple[int, tuple[Cell, ...]]] = []
        for offset, row in enumerate(_xlsx_rows(worksheet)):
            cells = tuple(OpenpyxlSheetReader._cell(cell) for cell in row)
            if offset == 0:
                header = cells
                continue
            rows.append((offset + 1, cells))
            # One extra row past the limit lets the parser report the overflow.
            if len(rows) > limits.max_rows:
                break
        header, *padded = _rectangular([header, *(cells for _number, cells in rows)])
        return header, [(number, cells) for (number, _), cells in zip(rows, padded, strict=True)]

    @staticmethod
    def _legacy_rows(
        data: bytes, limits: ImportLimits, sheet: str | None
    ) -> tuple[tuple[Cell, ...], ...]:
        """Rows of an old-format .xls; the sheet by name, or the first one."""
        with OpenpyxlSheetReader._xls(data, limits, on_demand=True) as book:
            worksheet = OpenpyxlSheetReader._legacy_sheet(book, sheet)
            rows = []
            for index in range(worksheet.nrows):
                rows.append(OpenpyxlSheetReader._legacy_row(book, worksheet, index))
                if len(rows) > limits.max_rows + MAX_EXPORT_HEADER_ROWS:
                    break
            return tuple(_rectangular(rows))

    @staticmethod
    def _legacy_sheet(book: xlrd.book.Book, sheet: str | None) -> xlrd.sheet.Sheet:
        if sheet is not None and sheet not in book.sheet_names():
            raise SheetMissing(sheet)
        return book.sheet_by_name(sheet) if sheet is not None else book.sheet_by_index(0)

    @staticmethod
    def _legacy_row(book: xlrd.book.Book, worksheet: xlrd.sheet.Sheet, index: int) -> tuple:
        """One row as stored: ragged rows end at their last cell, not at the sheet's."""
        width = min(worksheet.row_len(index), MAX_COLUMNS)
        return tuple(
            OpenpyxlSheetReader._legacy_cell(worksheet.cell(index, column), book.datemode)
            for column in range(width)
        )

    @staticmethod
    def _legacy_cell(cell: object, datemode: int) -> Cell:
        """One .xls cell in the same types an .xlsx gives: str, int, float, date, bool.

        A formula is not visible here — xlrd reports the value the file carries — so an
        .xls cannot be checked for formulas the way an .xlsx is.
        """
        kind, value = cell.ctype, cell.value
        if kind == xlrd.XL_CELL_EMPTY or kind == xlrd.XL_CELL_BLANK:
            return None
        if kind == xlrd.XL_CELL_TEXT:
            # A text that starts with "=" is still refused, as in an .xlsx.
            return FormulaCell() if value.startswith("=") else value
        if kind == xlrd.XL_CELL_BOOLEAN:
            return bool(value)
        if kind == xlrd.XL_CELL_ERROR:
            # #REF!, #DIV/0! and the like carry no value. They come back as an empty cell,
            # so the parser reports the field as missing — with its row and column — which
            # is what a person needs to fix the file (A on #51).
            return None
        if kind == xlrd.XL_CELL_DATE:
            try:
                parts = xlrd.xldate_as_tuple(value, datemode)
            except xlrd.XLDateError:
                # A date format over a number that is no date: negative, too large, or one
                # of 1900's first 60 days, which the format's leap-year bug leaves ambiguous.
                # The number comes back, and the parser reports the date as wrong.
                return int(value) if float(value).is_integer() else value
            if parts[:3] == (0, 0, 0):
                return time(*parts[3:])
            moment = datetime(*parts)
            return moment.date() if parts[3:] == (0, 0, 0) else moment
        # Numbers come back as floats; whole ones become int, as openpyxl would give.
        return int(value) if float(value).is_integer() else value

    @staticmethod
    def _cell(cell: object) -> Cell:
        value = getattr(cell, "value", None)
        # Detect formulas by data type and, defensively, by a leading "=".
        if getattr(cell, "data_type", None) == "f" or (
            isinstance(value, str) and value.startswith("=")
        ):
            return FormulaCell()
        return value
