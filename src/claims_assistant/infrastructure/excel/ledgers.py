"""S4-05: synthetic «Платежи» and «История долга» workbooks for demos and tests.

Same conventions as the «Контрагенты» template: fixed Russian headers, the INN column kept
as text, amounts written as numbers. Rows are given as raw cells so tests can also build
malformed sheets.
"""

import io
from collections.abc import Sequence
from datetime import date

from openpyxl import Workbook

from claims_assistant.domain import debt_history, interactions, payments
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.sheet_rules import Cell


def _workbook(sheet: str, titles: Sequence[str], rows: Sequence[Sequence[Cell]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(list(titles))
    for row in rows:
        worksheet.append(list(row))
    for cell in worksheet["A"]:
        cell.number_format = "@"  # INN stays text; leading digits are never lost
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def payment_cells(row: PaymentRow) -> list[Cell]:
    return [row.inn, row.payment_id, row.paid_on, float(row.amount)]


def snapshot_cells(row: DebtSnapshot) -> list[Cell]:
    return [row.inn, row.cutoff_date, float(row.debt)]


def build_payments_workbook(
    rows: Sequence[Sequence[Cell]], titles: Sequence[str] = payments.COLUMN_TITLES
) -> bytes:
    return _workbook(payments.SHEET_NAME, titles, rows)


def interaction_cells(row: InteractionRow) -> list[Cell]:
    return [row.inn, row.interaction_id, row.happened_on, row.comment, row.channel]


def build_interactions_workbook(
    rows: Sequence[Sequence[Cell]], titles: Sequence[str] = interactions.COLUMN_TITLES
) -> bytes:
    return _workbook(interactions.SHEET_NAME, titles, rows)


def build_debt_history_workbook(
    rows: Sequence[Sequence[Cell]], titles: Sequence[str] = debt_history.COLUMN_TITLES
) -> bytes:
    return _workbook(debt_history.SHEET_NAME, titles, rows)


# --- templates sent from the check dialog (S4-01): one synthetic row each ---

_SAMPLE_INN = "7707083893"  # the first sample row of the «Контрагенты» template


def build_payments_template() -> bytes:
    return build_payments_workbook([[_SAMPLE_INN, "PAY-0001", date(2026, 7, 15), 50000.00]])


def build_debt_history_template() -> bytes:
    return build_debt_history_workbook(
        [[_SAMPLE_INN, date(2026, 8, 1), 150000.00], [_SAMPLE_INN, date(2026, 9, 1), 150000.00]]
    )


def build_interactions_template() -> bytes:
    return build_interactions_workbook(
        [
            [
                _SAMPLE_INN,
                "INT-0001",
                date(2026, 8, 20),
                "Звонок: обещали оплатить до конца месяца.",
                "телефон",
            ]
        ]
    )
