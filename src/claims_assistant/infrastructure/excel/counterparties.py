"""S2-04: write a synthetic «Контрагенты» template for demos and tests.

Values are synthetic; INNs are generated with a valid control digit so the template
round-trips cleanly through the reader and parser.
"""

import io
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from openpyxl import Workbook

from claims_assistant.domain.counterparties import (
    COLUMN_TITLES,
    SHEET_NAME,
    CounterpartyRow,
)

_LEGAL_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)


def _with_control_digit(prefix: str) -> str:
    control = sum(w * int(d) for w, d in zip(_LEGAL_WEIGHTS, prefix, strict=True)) % 11 % 10
    return prefix + str(control)


def _sample_rows() -> tuple[CounterpartyRow, ...]:
    return (
        # Minimal mode: only the INN is known.
        CounterpartyRow(inn=_with_control_digit("770708389")),
        # Full row with debt, overdue days and payment history on a cut-off date.
        CounterpartyRow(
            inn=_with_control_digit("771014067"),
            name="ООО «Синтетический контрагент»",
            cutoff_date=date(2026, 9, 1),
            debt=Decimal("150000.00"),
            overdue_days=45,
            last_payment_date=date(2026, 7, 15),
        ),
    )


def _cell_values(row: CounterpartyRow) -> list[object]:
    return [
        row.inn,
        row.name,
        row.cutoff_date,
        None if row.debt is None else float(row.debt),
        row.overdue_days,
        row.last_payment_date,
    ]


def build_counterparties_template(rows: Sequence[CounterpartyRow] | None = None) -> bytes:
    """Return the bytes of an .xlsx with the fixed header and the given (or sample) rows."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = SHEET_NAME
    worksheet.append(list(COLUMN_TITLES))
    # Keep the INN column textual so leading digits are never lost.
    for cell in worksheet["A"]:
        cell.number_format = "@"
    for row in _sample_rows() if rows is None else rows:
        worksheet.append(_cell_values(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
