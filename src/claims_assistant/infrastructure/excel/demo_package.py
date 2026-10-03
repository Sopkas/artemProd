"""S6-04: the demo package as files the bot can actually be given.

``infrastructure/demo/package.py`` says what the four counterparties are; this renders
them into the four workbooks of the data contract, with the same headers and the same
conventions as the templates the dialog sends out. Nothing is special-cased for the demo:
if these files stopped being readable, so would a customer's.

The «Платежи» sheet of «Северный путь» is deliberately empty of its rows — the export
covers June to the analysis date and contains no payment for it. That emptiness is the
evidence, which is why the period is stated when the file is attached and not guessed.

The files are the same bytes on every build. openpyxl stamps the time of saving into the
document properties and the zip entries, and a package is accepted by its sha256: a file
rebuilt and sent again into the same check must be recognised as the one already there,
not land a second time (review A on #61). ``_frozen`` takes the clock out.
"""

import io
import re
import zipfile

from claims_assistant.domain import debt_history, interactions, payments
from claims_assistant.infrastructure.demo.package import (
    ANALYSIS_DATE,
    COMPANIES,
    PAYMENTS_FROM,
    PAYMENTS_TO,
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

_HISTORY_DATES = sorted({row.cutoff_date for row in debt_snapshots()})

FILE_NOTES = {
    COUNTERPARTIES_FILE: f"обязательный файл проверки, контрагентов: {len(COMPANIES)}",
    PAYMENTS_FILE: "необязательный: платежи, период полноты — "
    f"{PAYMENTS_FROM:%d.%m.%Y}–{PAYMENTS_TO:%d.%m.%Y}",
    HISTORY_FILE: "необязательный: долг на "
    + " и на ".join(f"{day:%d.%m.%Y}" for day in _HISTORY_DATES),
    INTERACTIONS_FILE: "необязательный: звонки и письма",
}

# The moment written instead of the time of saving: the package's own analysis date.
FROZEN_AT = (ANALYSIS_DATE.year, ANALYSIS_DATE.month, ANALYSIS_DATE.day, 0, 0, 0)
_STAMP = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(*FROZEN_AT)
_CORE = "docProps/core.xml"
_SAVED_AT = re.compile(rb"(<dcterms:(created|modified)\b[^>]*>)[^<]*(</dcterms:\2>)")


def _frozen(workbook: bytes) -> bytes:
    """The same workbook without the time it was saved at: the properties and every zip
    entry carry ``FROZEN_AT``. openpyxl has no setting for it, so the archive is repacked."""
    result = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(workbook)) as source,
        zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == _CORE:
                data = _SAVED_AT.sub(rb"\g<1>" + _STAMP.encode() + rb"\g<3>", data)
            entry = zipfile.ZipInfo(item.filename, date_time=FROZEN_AT)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = item.external_attr
            target.writestr(entry, data)
    return result.getvalue()


def build_demo_package() -> dict[str, bytes]:
    """The four workbooks of the demo, keyed by the name they are saved under."""
    return {name: _frozen(data) for name, data in _workbooks().items()}


def _workbooks() -> dict[str, bytes]:
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
