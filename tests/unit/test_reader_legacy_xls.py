"""S4-05: the old .xls format, which the customer's 1C prints come in.

The fixture is synthetic and was written once with xlwt; it holds a cell of every kind we
have to recognise. The point of these tests is that an .xls gives the same cell types an
.xlsx does — and that the one thing it cannot give (a formula) is stated, not glossed over.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from claims_assistant.application.imports import CorruptWorkbook, SheetMissing, WorkbookTooLarge
from claims_assistant.domain.counterparties import FormulaCell, ImportLimits
from claims_assistant.infrastructure.excel.ledgers import build_payments_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader, is_legacy_xls

LEGACY = Path(__file__).parent.parent / "fixtures" / "excel" / "legacy.xls"
SHEET = "Взаимодействия"


@pytest.fixture
def data() -> bytes:
    return LEGACY.read_bytes()


def rows(data: bytes, limits: ImportLimits | None = None):
    return OpenpyxlSheetReader().read_rows(data, limits or ImportLimits())


def test_the_two_formats_are_told_apart_by_their_own_bytes(data):
    assert is_legacy_xls(data) is True
    assert is_legacy_xls(build_payments_template()) is False
    assert is_legacy_xls(b"") is False


def test_cells_come_back_in_the_same_types_an_xlsx_gives(data):
    sheet = rows(data)
    assert sheet[0][:2] == ("ИНН", "ID взаимодействия")
    assert sheet[1][2] == date(2026, 8, 20)  # a date cell stays a date
    assert sheet[2][1] == 42 and type(sheet[2][1]) is int  # a whole number is an int
    assert sheet[2][2] == datetime(2026, 8, 21, 14, 30)  # time is kept when there is time
    assert sheet[2][4] is None  # an empty cell is None, not ""
    assert sheet[3][4] is True
    assert sheet[4][1] == 1500.75


def test_a_text_that_starts_with_an_equals_sign_is_still_refused(data):
    assert isinstance(rows(data)[3][3], FormulaCell)


def test_a_real_formula_cannot_be_seen_in_this_format(data):
    """The limitation, written down: .xls carries the value, not the formula.

    xlrd hands over what the file stores, so a formula cell arrives as an ordinary value
    and the «замените формулу на значение» rule cannot be enforced here. Callers warn the
    user instead (docs/data-contracts.md).
    """
    formula_cell = rows(data)[4][0]
    assert not isinstance(formula_cell, FormulaCell)


def test_a_sheet_is_read_by_name_and_a_missing_one_is_reported(data):
    header, body = OpenpyxlSheetReader().read(data, SHEET, ImportLimits())
    assert header[0] == "ИНН" and body[0][0] == 2  # excel row numbers start after the header
    with pytest.raises(SheetMissing):
        OpenpyxlSheetReader().read(data, "Платежи", ImportLimits())


def test_a_broken_file_and_an_oversized_one_are_refused(data):
    with pytest.raises(CorruptWorkbook):
        rows(data[:8] + b"not really a workbook")
    with pytest.raises(WorkbookTooLarge):
        rows(data, ImportLimits(max_unpacked_bytes=16))
