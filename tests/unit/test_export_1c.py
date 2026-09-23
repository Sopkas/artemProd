"""S4-05: the customer's own 1C export of payments (docs/data-contracts.md §2).

The shape here is the one the customer sent on 23.09.2026 — a parameters block, a table
that starts lower down, «Поступление»/«Списание» instead of one amount, a document line
instead of an id and a totals row at the end. The values are synthetic: no file of the
customer is stored in the repository.
"""

from datetime import date
from decimal import Decimal

import pytest

from claims_assistant.application.ledger_imports import import_payments_export
from claims_assistant.domain.counterparties import ImportLimits
from claims_assistant.domain.export_1c import (
    convert_payments,
    find_table,
    payment_id,
    read_export_header,
)
from claims_assistant.domain.external import Period

INN = "1234567894"
DAY = date(2026, 9, 23)
TITLES = (
    "Дата",
    "Документ",
    "Вид операции",
    "Назначение платежа",
    "Банковский счет",
    "Поступление",
    "Списание",
)


def export_rows(payments=None, *, start="01.01.2000 0:00:00", end="22.09.2026 9:38:50"):
    """The print as 1C lays it out: the block, the titles, the rows, the total."""
    payments = (
        payments
        if payments is not None
        else [
            (
                "02.09.2024",
                "Поступление на расчетный счет 00БП-036835 от 02.09.2024 17:00:38",
                "Оплата от покупателя",
                "Обеспечительный платеж",
                "Счёт 1",
                2258000,
                None,
            ),
            (
                "12.09.2024",
                "Поступление на расчетный счет 00БП-036902 от 12.09.2024 10:05:11",
                "Оплата от покупателя",
                "Оплата ежемесячного платежа",
                "Счёт 1",
                517325.82,
                None,
            ),
        ]
    )
    total = sum(p[5] for p in payments if isinstance(p[5], (int, float)))
    return [
        (None,) * 7,
        ("Параметры:", None, "Контрагент: СИНТЕТИКА ООО", None, None, None, None),
        (None, None, "Организация: НАША ФИРМА ООО", None, None, None, None),
        (None, None, f"Дата начала: {start}", None, None, None, None),
        (None, None, f"Дата окончания: {end}", None, None, None, None),
        (None,) * 7,
        TITLES,
        *payments,
        ("Итого", total, None, None, None, None, None),
    ]


class RowsReader:
    """Stands in for the Excel reader: hands back the rows it was given."""

    def __init__(self, rows, error=None):
        self._rows, self._error = rows, error

    def read_rows(self, source, limits, sheet=None):
        if self._error is not None:
            raise self._error
        return tuple(self._rows)


def run(rows=None, inn=INN, analysis_date=DAY):
    return import_payments_export(
        RowsReader(rows if rows is not None else export_rows()),
        b"",
        inn=inn,
        analysis_date=analysis_date,
        limits=ImportLimits(),
    )


def test_the_export_is_read_as_the_payments_sheet():
    result, export = run()
    assert [row.payment_id for row in result.rows] == ["00БП-036835", "00БП-036902"]
    assert [row.paid_on for row in result.rows] == [date(2024, 9, 2), date(2024, 9, 12)]
    assert [row.amount for row in result.rows] == [Decimal("2258000"), Decimal("517325.82")]
    assert all(row.inn == INN for row in result.rows)  # the dialog said whose file it is
    assert result.issues == ()
    assert export.counterparty == "СИНТЕТИКА ООО" and export.organization == "НАША ФИРМА ООО"


def test_the_covered_period_comes_from_the_export_header():
    _, export = run()
    assert export.period == Period(date(2000, 1, 1), date(2026, 9, 22))
    # A header without the dates gives no period, and nothing is invented.
    rows = [row for row in export_rows() if "Дата начала" not in str(row[2])]
    assert run(rows)[1].period is None


def test_a_table_that_starts_on_the_first_row_has_no_header_block():
    """Found by A on #50: index 0 is falsy, so the payments themselves were read as the
    parameters block and gave a period and a counterparty out of nowhere."""
    rows = [
        TITLES,
        (
            "02.09.2024",
            "Поступление 00БП-1 от 02.09.2024",
            "",
            "Дата начала: 01.01.2020",
            "",
            1000,
            None,
        ),
    ]
    result, export = run(rows)
    assert [row.payment_id for row in result.rows] == ["00БП-1"]
    assert export.period is None and export.counterparty is None


def test_the_totals_row_is_not_a_payment():
    result, _ = run()
    assert len(result.rows) == 2
    assert all(row.amount != Decimal("2775325.82") for row in result.rows)


def test_an_outgoing_payment_is_skipped_and_reported():
    rows = export_rows(
        [
            (
                "02.09.2024",
                "Поступление на расчетный счет 00БП-1 от 02.09.2024",
                "Оплата от покупателя",
                "Платёж",
                "Счёт 1",
                1000,
                None,
            ),
            (
                "03.09.2024",
                "Списание с расчетного счета 00БП-2 от 03.09.2024",
                "Возврат покупателю",
                "Возврат",
                "Счёт 1",
                None,
                500,
            ),
        ]
    )
    result, _ = run(rows)
    assert [row.payment_id for row in result.rows] == ["00БП-1"]
    assert [issue.code for issue in result.issues] == ["payment_is_outgoing"]
    assert result.issues[0].row == 9 and "только поступления" in result.issues[0].reason


def test_a_file_without_the_table_is_refused_without_guessing():
    result, export = run([(None, None), ("Параметры:", "Контрагент: СИНТЕТИКА ООО")])
    assert result.rows == ()
    assert [issue.code for issue in result.issues] == ["export_table_missing"]
    assert export.counterparty == "СИНТЕТИКА ООО"  # the block is still read


def test_the_contract_rules_still_apply_to_the_rows():
    """The adapter only reshapes; the payments parser judges the values as always."""
    rows = export_rows(
        [
            ("02.09.2024", "Поступление 00БП-1 от 02.09.2024", "", "", "", 1000, None),
            ("40.13.2024", "Поступление 00БП-2 от 40.13.2024", "", "", "", 1000, None),
            ("03.09.2024", "Поступление 00БП-3 от 03.09.2024", "", "", "", -5, None),
            ("04.10.2026", "Поступление 00БП-4 от 04.10.2026", "", "", "", 700, None),
        ]
    )
    result, _ = run(rows)
    codes = {issue.code for issue in result.issues}
    assert "date_invalid" in codes  # a broken date is reported, not guessed
    assert "payment_amount_negative" in codes
    assert "payment_after_analysis_date" in codes  # later than the analysis date
    assert [row.payment_id for row in result.rows] == ["00БП-1"]


@pytest.mark.parametrize(
    "document,expected",
    [
        ("Поступление на расчетный счет 00БП-036835 от 02.09.2024 17:00:38", "00БП-036835"),
        ("Платёжное поручение №12 от 01.01.2026", "Платёжное поручение №12 от 01.01.2026"),
        (None, None),
    ],
)
def test_the_document_line_gives_the_payment_id(document, expected):
    assert payment_id(document) == expected


def test_the_table_is_found_below_the_block_and_nowhere_else():
    rows = export_rows()
    assert find_table(rows) == 6
    assert find_table([(None, None), ("Просто текст", None)]) is None
    assert read_export_header(rows[:6]).counterparty == "СИНТЕТИКА ООО"


def test_convert_needs_no_reader_and_keeps_the_sheet_name():
    converted = convert_payments(export_rows(), INN)
    assert converted.header == ("ИНН", "ID платежа", "Дата платежа", "Сумма платежа")
    assert len(converted.rows) == 2 and converted.rows[0][0] == 8  # the first data row
