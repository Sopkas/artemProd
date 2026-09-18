import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook

from claims_assistant.application.imports import (
    CorruptWorkbook,
    SheetMissing,
    WorkbookTooLarge,
    import_counterparties,
)
from claims_assistant.domain.counterparties import (
    COLUMN_TITLES,
    SHEET_NAME,
    FormulaCell,
    ImportLimits,
    parse_counterparties,
)
from claims_assistant.domain.imports import IssueSeverity
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader

VALID_INN = "7707083893"
OTHER_INN = "7710140679"
HEADERS = COLUMN_TITLES


def rows(*data):
    """Build (row_number, cells) pairs starting at Excel row 2."""
    return [(index + 2, tuple(cells)) for index, cells in enumerate(data)]


def parse(header, *data, **kwargs):
    return parse_counterparties(tuple(header), rows(*data), **kwargs)


def errors(result):
    return [i for i in result.issues if i.severity is IssueSeverity.ERROR]


def warnings(result):
    return [i for i in result.issues if i.severity is IssueSeverity.WARNING]


def codes(result):
    return {i.code for i in result.issues}


# --- header mapping -----------------------------------------------------------


def test_minimal_and_full_rows_parse():
    result = parse(
        HEADERS,
        [VALID_INN, None, None, None, None, None],
        [OTHER_INN, "ООО Ромашка", date(2026, 9, 1), 1500.5, 10, date(2026, 8, 1)],
    )
    assert not result.issues
    assert result.has_usable_rows
    assert [r.inn for r in result.rows] == [VALID_INN, OTHER_INN]
    full = result.rows[1]
    assert full.name == "ООО Ромашка"
    assert full.debt == Decimal("1500.5")
    assert full.overdue_days == 10
    assert full.cutoff_date == date(2026, 9, 1)


def test_unknown_column_is_a_warning_not_an_error():
    result = parse(["ИНН", "Телефон"], [VALID_INN, "+7999"])
    assert [r.inn for r in result.rows] == [VALID_INN]
    assert not errors(result)
    assert warnings(result) and warnings(result)[0].code == "unknown_column"
    assert "Телефон" in warnings(result)[0].reason


def test_missing_inn_column_is_a_file_error():
    result = parse(["Наименование"], ["ООО Ромашка"])
    assert not result.rows
    issue = errors(result)[0]
    assert issue.code == "inn_column_missing"
    assert issue.sheet == SHEET_NAME
    assert issue.row is None and issue.column is None


def test_duplicate_column_is_fatal():
    result = parse(["ИНН", "ИНН"], [VALID_INN, VALID_INN])
    assert not result.rows
    assert "duplicate_column" in codes(result)


# --- INN normalization --------------------------------------------------------


def test_numeric_inn_is_recovered_exactly():
    result = parse(["ИНН"], [int(VALID_INN)])
    assert [r.inn for r in result.rows] == [VALID_INN]


@pytest.mark.parametrize(
    "value,code",
    [
        ("12345678901", "inn_invalid"),  # wrong length
        ("123456789012", "inn_invalid"),  # 12 digits: unsupported counterparty type
        ("7707083894", "inn_invalid"),  # broken control digit
        (7707083893.5, "inn_invalid"),  # fractional number is not an exact integer
        (770708389, "inn_invalid"),  # nine digits: lost digits are never guessed
        (None, "inn_missing"),
        ("", "inn_missing"),
        (True, "inn_invalid"),
    ],
)
def test_bad_inn_drops_the_row_with_coordinate(value, code):
    result = parse(["ИНН", "Наименование"], [value, "ООО Ромашка"])
    assert not result.rows
    assert len(errors(result)) == 1
    issue = errors(result)[0]
    assert issue.code == code
    assert issue.column == "A"
    assert issue.row == 2


def test_error_reason_never_echoes_the_raw_inn():
    raw = "7707083894"
    result = parse(["ИНН"], [raw])
    assert errors(result) and raw not in errors(result)[0].reason


# --- value types --------------------------------------------------------------


@pytest.mark.parametrize(
    "field_index,value,ok",
    [
        (3, -1, False),  # negative debt
        (3, 100.0, True),
        (3, "1 000,50", True),  # spaces and decimal comma
        (3, "1.234", False),  # sub-kopeck precision
        (3, "не число", False),
        (4, 5, True),  # overdue days
        (4, -5, False),
        (4, 5.5, False),
    ],
)
def test_debt_and_overdue_rules(field_index, value, ok):
    cells = [OTHER_INN, None, date(2026, 9, 1), None, None, None]
    cells[field_index] = value
    result = parse(HEADERS, cells)
    assert bool(result.rows) is ok
    assert bool(errors(result)) is not ok


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-01", date(2026, 9, 1)),
        ("01.09.2026", date(2026, 9, 1)),
        (datetime(2026, 9, 1, 12, 0), date(2026, 9, 1)),
    ],
)
def test_accepted_date_formats(value, expected):
    result = parse(["ИНН", "Дата среза", "Сумма долга"], [VALID_INN, value, 10.0])
    assert result.rows[0].cutoff_date == expected


@pytest.mark.parametrize("value", ["01/09/2026", "2026.09.01", "1 сентября", "2026-13-01"])
def test_ambiguous_or_invalid_dates_are_rejected(value):
    result = parse(["ИНН", "Дата среза", "Сумма долга"], [VALID_INN, value, 10.0])
    assert not result.rows and "date_invalid" in codes(result)


def test_cutoff_required_when_debt_present():
    result = parse(["ИНН", "Сумма долга"], [VALID_INN, 100.0])
    assert not result.rows
    assert "cutoff_required" in codes(result)


def test_analysis_date_mismatch_and_future_payment():
    result = parse(
        HEADERS,
        [VALID_INN, None, date(2026, 9, 2), 100.0, None, date(2026, 12, 1)],
        analysis_date=date(2026, 9, 1),
    )
    assert {"cutoff_mismatch", "last_payment_future"} <= codes(result)


# --- duplicates, conflicts, formulas, limits ----------------------------------


def test_identical_rows_merge_with_warning():
    result = parse(
        ["ИНН", "Наименование"],
        [VALID_INN, "ООО Ромашка"],
        [VALID_INN, "ООО Ромашка"],
    )
    assert [r.inn for r in result.rows] == [VALID_INN]
    assert warnings(result) and warnings(result)[0].code == "duplicate_row"


def test_conflicting_rows_are_an_error_and_not_summed():
    result = parse(
        ["ИНН", "Наименование"],
        [VALID_INN, "ООО Ромашка"],
        [VALID_INN, "ООО Одуванчик"],
    )
    assert [r.inn for r in result.rows] == [VALID_INN]
    assert result.rows[0].name == "ООО Ромашка"
    assert "inn_conflict" in codes(result)


def test_formula_cells_are_rejected():
    result = parse(
        ["ИНН", "Сумма долга", "Дата среза"], [VALID_INN, FormulaCell(), date(2026, 9, 1)]
    )
    assert not result.rows
    assert "formula_forbidden" in codes(result)


def test_row_limit_is_enforced():
    data = [[VALID_INN]] * 5
    result = parse_counterparties(("ИНН",), rows(*data), ImportLimits(max_rows=3))
    assert "row_limit" in codes(result)


# --- reader + pipeline over real .xlsx bytes ----------------------------------


def make_xlsx(header, data, sheet=SHEET_NAME):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(list(header))
    for row in data:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_template_round_trips_through_reader_and_parser():
    data = build_counterparties_template()
    result = import_counterparties(OpenpyxlSheetReader(), data)
    assert not result.issues
    assert len(result.rows) == 2
    assert result.rows[1].debt == Decimal("150000.00")


def test_reader_recovers_numeric_inn_from_xlsx():
    data = make_xlsx(HEADERS, [[int(VALID_INN), None, None, None, None, None]])
    result = import_counterparties(OpenpyxlSheetReader(), data)
    assert [r.inn for r in result.rows] == [VALID_INN]


def test_formula_in_real_xlsx_is_detected():
    data = make_xlsx(HEADERS, [[VALID_INN, None, "2026-09-01", "=1+1", None, None]])
    result = import_counterparties(OpenpyxlSheetReader(), data)
    assert not result.rows
    assert "formula_forbidden" in codes(result)


def test_corrupt_file_becomes_a_file_error():
    result = import_counterparties(OpenpyxlSheetReader(), b"definitely not a workbook")
    assert not result.rows
    issue = errors(result)[0]
    assert issue.code == "workbook_corrupt"
    assert issue.reason == CorruptWorkbook.reason


def test_missing_sheet_is_reported():
    data = make_xlsx(HEADERS, [[VALID_INN]], sheet="Другой лист")
    result = import_counterparties(OpenpyxlSheetReader(), data)
    assert not result.rows
    assert errors(result)[0].code == "sheet_missing"
    assert SHEET_NAME in errors(result)[0].reason


def test_oversized_archive_is_rejected():
    data = build_counterparties_template()
    result = import_counterparties(
        OpenpyxlSheetReader(), data, ImportLimits(max_rows=10, max_unpacked_bytes=10)
    )
    assert not result.rows
    issue = errors(result)[0]
    assert issue.code == "workbook_too_large"
    assert issue.reason == WorkbookTooLarge.reason


def test_reader_raises_typed_errors_directly():
    reader = OpenpyxlSheetReader()
    with pytest.raises(CorruptWorkbook):
        reader.read(b"not a zip", SHEET_NAME, ImportLimits())
    with pytest.raises(SheetMissing):
        reader.read(make_xlsx(HEADERS, [], sheet="Иное"), SHEET_NAME, ImportLimits())
