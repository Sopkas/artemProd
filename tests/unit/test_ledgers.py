"""S4-05: «Платежи» and «История долга» — parsed through the real reader from .xlsx bytes."""

from datetime import date
from decimal import Decimal

import pytest

from claims_assistant.application.ledger_imports import import_debt_history, import_payments
from claims_assistant.domain.imports import IssueSeverity
from claims_assistant.domain.sheet_rules import parse_amount
from claims_assistant.infrastructure.excel.ledgers import (
    build_debt_history_workbook,
    build_payments_workbook,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader

INN = "1234567894"
OTHER = "7707083893"  # valid INN, not in the package
PACKAGE = frozenset({INN})
DAY = date(2026, 9, 1)
NBSP_AMOUNT = "15" + chr(0x00A0) + "000,50"  # «15 000,50» with a no-break space, as 1C exports


def payments(rows, titles=None, known=PACKAGE):
    data = build_payments_workbook(rows, **({"titles": titles} if titles else {}))
    return import_payments(OpenpyxlSheetReader(), data, known_inns=known, analysis_date=DAY)


def history(rows, titles=None, known=PACKAGE):
    data = build_debt_history_workbook(rows, **({"titles": titles} if titles else {}))
    return import_debt_history(OpenpyxlSheetReader(), data, known_inns=known, analysis_date=DAY)


def codes(result):
    return [(i.code, i.severity, i.row, i.column) for i in result.issues]


# --- payments ---


def test_valid_payments_round_trip():
    result = payments(
        [
            [INN, "P-1", date(2026, 8, 1), 1000.0],
            [INN, 555, "15.08.2026", NBSP_AMOUNT],  # numeric ID, Russian date, NBSP amount
        ]
    )
    assert result.issues == ()
    first, second = result.rows
    assert (first.payment_id, first.paid_on, first.amount) == (
        "P-1",
        date(2026, 8, 1),
        Decimal("1000"),
    )
    assert (second.payment_id, second.amount) == ("555", Decimal("15000.50"))


def test_two_payments_with_the_same_date_and_amount_but_different_ids_are_both_kept():
    result = payments([[INN, "A", DAY, 10.0], [INN, "B", DAY, 10.0]])
    assert len(result.rows) == 2 and result.issues == ()


@pytest.mark.parametrize(
    "row,code,column",
    [
        ([None, "P", DAY, 1.0], "inn_missing", "A"),
        (["0000000001", "P", DAY, 1.0], "inn_invalid", "A"),
        ([INN, None, DAY, 1.0], "payment_id_missing", "B"),
        ([INN, 1.5, DAY, 1.0], "payment_id_invalid", "B"),
        ([INN, "P", None, 1.0], "payment_date_missing", "C"),
        ([INN, "P", "01/08/2026", 1.0], "date_invalid", "C"),
        ([INN, "P", DAY, None], "payment_amount_missing", "D"),
        ([INN, "P", DAY, 0.0], "payment_amount_zero", "D"),
        ([INN, "P", DAY, -5.0], "payment_amount_negative", "D"),
        ([INN, "P", DAY, "много"], "payment_amount_not_number", "D"),
        ([INN, "P", DAY, 1.005], "payment_amount_precision", "D"),
        ([INN, "P", DAY, "=1+1"], "formula_forbidden", "D"),
    ],
)
def test_bad_payment_cells_are_errors_with_their_column(row, code, column):
    result = payments([row])
    assert result.rows == ()
    assert (code, IssueSeverity.ERROR, 2, column) in codes(result)


def test_payment_of_an_inn_outside_the_package_is_rejected():
    result = payments([[OTHER, "P", DAY, 1.0]])
    assert result.rows == ()
    assert codes(result) == [("inn_not_in_package", IssueSeverity.ERROR, 2, "A")]
    assert OTHER not in result.issues[0].reason


def test_payment_after_the_analysis_date_is_excluded_with_a_warning():
    result = payments([[INN, "P", date(2026, 9, 2), 1.0]])
    assert result.rows == ()
    assert codes(result) == [("payment_after_analysis_date", IssueSeverity.WARNING, 2, "C")]


def test_repeated_payment_id_same_data_warns_other_data_errors():
    result = payments(
        [
            [INN, "P", DAY, 10.0],
            [INN, "P", DAY, 10.0],  # same payment again
            [INN, "P", DAY, 99.0],  # same ID, different amount
        ]
    )
    assert len(result.rows) == 1
    assert ("duplicate_payment", IssueSeverity.WARNING, 3, None) in codes(result)
    assert ("payment_id_conflict", IssueSeverity.ERROR, 4, "B") in codes(result)


def test_same_payment_id_for_two_different_inns_is_two_payments():
    second = "7700000009"
    result = payments([[INN, "P", DAY, 1.0], [second, "P", DAY, 1.0]], known=None)
    assert len(result.rows) == 2


def test_missing_required_column_makes_the_sheet_unusable():
    result = payments([[INN, "P", DAY]], titles=["ИНН", "ID платежа", "Дата платежа"])
    assert result.rows == ()
    assert ("amount_column_missing", IssueSeverity.ERROR, None, None) in codes(result)


def test_unknown_column_is_a_warning():
    titles = ["ИНН", "ID платежа", "Дата платежа", "Сумма платежа", "Комментарий"]
    result = payments([[INN, "P", DAY, 1.0, "x"]], titles=titles)
    assert len(result.rows) == 1
    assert ("unknown_column", IssueSeverity.WARNING, 1, "E") in codes(result)


def test_missing_sheet_is_one_file_level_issue():
    data = build_debt_history_workbook([[INN, DAY, 1.0]])  # no «Платежи» sheet inside
    result = import_payments(OpenpyxlSheetReader(), data, known_inns=PACKAGE, analysis_date=DAY)
    assert codes(result) == [("sheet_missing", IssueSeverity.ERROR, None, None)]


def test_corrupt_file_is_one_file_level_issue():
    result = import_payments(
        OpenpyxlSheetReader(), b"not a zip", known_inns=PACKAGE, analysis_date=DAY
    )
    assert codes(result) == [("workbook_corrupt", IssueSeverity.ERROR, None, None)]


# --- debt history ---


def test_valid_history_round_trip():
    result = history([[INN, date(2026, 7, 31), 1000.0], [INN, "31.08.2026", 0.0]])
    assert result.issues == ()
    assert [(r.cutoff_date, r.debt) for r in result.rows] == [
        (date(2026, 7, 31), Decimal("1000")),
        (date(2026, 8, 31), Decimal("0")),  # zero is a known value, not a gap
    ]


@pytest.mark.parametrize(
    "row,code,column",
    [
        ([INN, None, 1.0], "cutoff_missing", "B"),
        ([INN, DAY, None], "debt_missing", "C"),
        ([INN, DAY, -1.0], "debt_negative", "C"),
        ([INN, "2026-02-30", 1.0], "date_invalid", "B"),
    ],
)
def test_bad_history_cells_are_errors_with_their_column(row, code, column):
    result = history([row])
    assert result.rows == ()
    assert (code, IssueSeverity.ERROR, 2, column) in codes(result)


def test_history_duplicates_merge_and_conflicts_are_errors():
    result = history(
        [
            [INN, DAY, 100.0],
            [INN, DAY, 100.0],  # identical: merged
            [INN, DAY, 250.0],  # same INN and date, other amount
        ]
    )
    assert len(result.rows) == 1
    assert ("duplicate_row", IssueSeverity.WARNING, 3, None) in codes(result)
    assert ("snapshot_conflict", IssueSeverity.ERROR, 4, "C") in codes(result)


def test_history_outside_the_package_or_after_the_analysis_date():
    result = history([[OTHER, DAY, 1.0], [INN, date(2026, 9, 2), 1.0]])
    assert result.rows == ()
    assert codes(result) == [
        ("inn_not_in_package", IssueSeverity.ERROR, 2, "A"),
        ("cutoff_after_analysis_date", IssueSeverity.WARNING, 3, "B"),
    ]


# --- shared amount rule (fixes S2-04: a no-break space was not accepted) ---


@pytest.mark.parametrize("separator", [" ", chr(0x00A0), chr(0x202F), chr(0x2009)])
def test_amounts_accept_every_group_separator(separator):
    text = f"150{separator}000,00"
    assert parse_amount(text, prefix="debt", label="Сумма долга") == Decimal("150000.00")
