"""S6-04: the demo package as files the bot can actually be given.

``infrastructure/demo/package.py`` says what the four counterparties are; this renders
them into the four workbooks of the data contract, with the same headers and the same
conventions as the templates the dialog sends out. Nothing is special-cased for the demo:
if these files stopped being readable, so would a customer's.

The «Платежи» sheet of «Северный путь» is deliberately empty of its rows — the export
covers June to the analysis date and contains no payment for it. That emptiness is the
evidence, which is why the period is stated when the file is attached and not guessed.
"""

from claims_assistant.domain import debt_history, interactions, payments
from claims_assistant.infrastructure.demo.package import (
    COMPANIES,
    counterparty_rows,
    debt_snapshots,
    interaction_rows,
    payment_rows,
)
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import (
    build_debt_history_workbook,
    build_interactions_workbook,
    build_payments_workbook,
    interaction_cells,
    payment_cells,
    snapshot_cells,
)

COUNTERPARTIES_FILE = "demo-kontragenty.xlsx"
PAYMENTS_FILE = "demo-platezhi.xlsx"
HISTORY_FILE = "demo-istoriya-dolga.xlsx"
INTERACTIONS_FILE = "demo-vzaimodeystviya.xlsx"

FILE_NOTES = {
    COUNTERPARTIES_FILE: f"обязательный файл проверки, {len(COMPANIES)} контрагента",
    PAYMENTS_FILE: "необязательный: платежи, период полноты — 01.06.2026–01.09.2026",
    HISTORY_FILE: "необязательный: долг на 01.08.2026 и на 01.09.2026",
    INTERACTIONS_FILE: "необязательный: звонки и письма",
}


def build_demo_package() -> dict[str, bytes]:
    """The four workbooks of the demo, keyed by the name they are saved under."""
    return {
        COUNTERPARTIES_FILE: build_counterparties_template(counterparty_rows()),
        PAYMENTS_FILE: build_payments_workbook(
            [payment_cells(row) for row in payment_rows()], payments.COLUMN_TITLES
        ),
        HISTORY_FILE: build_debt_history_workbook(
            [snapshot_cells(row) for row in debt_snapshots()], debt_history.COLUMN_TITLES
        ),
        INTERACTIONS_FILE: build_interactions_workbook(
            [interaction_cells(row) for row in interaction_rows()], interactions.COLUMN_TITLES
        ),
    }
