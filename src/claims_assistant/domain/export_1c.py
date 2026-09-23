"""S4-05: the customer's own 1C export of payments, turned into our sheet (contract §2).

The export the customer sent (23.09.2026) is not our template and does not have to be:
it is what 1C prints for one counterparty. This module translates it, so the parser in
``domain/payments.py`` keeps working on the contract's own shape and the quirks of the
source live in one place.

What the real file looks like::

    Параметры:            Контрагент: <название>
                          Организация: <название>
                          Дата начала: 01.01.2000 0:00:00
                          Дата окончания: 22.09.2026 9:38:50
    Дата | Документ | Вид операции | Назначение платежа | Банковский счет | Поступление | Списание
    02.09.2024 | Поступление на расчетный счет 00БП-036835 от 02.09.2024 17:00:38 | … | 2258000 |
    …
    Итого | 37643411.55

Three things it does not have and one it gives for free:

- **no ИНН** — the file is per counterparty, named in the header only; the INN comes from
  the dialog, where the user already said whose file this is;
- **no payment id** — the number inside «Документ» («00БП-036835») serves as one;
- **no single amount** — receipts are in «Поступление», and a row with «Списание» is an
  outgoing payment, which the contract does not accept yet: it is skipped and reported;
- **the covered period is in the header** («Дата начала»/«Дата окончания»), so the user
  does not have to type it.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from claims_assistant.domain.external import Period
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.payments import COLUMN_TITLES as PAYMENT_TITLES
from claims_assistant.domain.payments import SHEET_NAME as PAYMENTS_SHEET
from claims_assistant.domain.sheet_rules import Cell, FormulaCell, issue

MAX_HEADER_ROWS = 20  # the header block of a 1C print is short; beyond that we give up
TOTALS_WORD = "итого"
_DOCUMENT_NUMBER = re.compile(r"[0-9A-ZА-ЯЁ]+-[0-9]+")
_HEADER_VALUE = re.compile(r"^\s*([^:]{2,40}):\s*(.+?)\s*$")
_DATE_TEXT = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})")

# Titles of the export's own columns; everything else on the row is ignored.
DATE_TITLE = "дата"
DOCUMENT_TITLE = "документ"
INCOMING_TITLE = "поступление"
OUTGOING_TITLE = "списание"
PURPOSE_TITLE = "назначение платежа"


@dataclass(frozen=True, slots=True)
class ExportHeader:
    """What the block above the table says about the export."""

    counterparty: str | None = None
    organization: str | None = None
    period: Period | None = None


@dataclass(frozen=True, slots=True)
class ConvertedSheet:
    """The export in the contract's own shape, ready for ``parse_payments``."""

    header: tuple[Cell, ...]
    rows: tuple[tuple[int, tuple[Cell, ...]], ...]
    export: ExportHeader
    issues: tuple[ImportIssue, ...] = ()


def _text(cell: Cell) -> str | None:
    if isinstance(cell, FormulaCell) or not isinstance(cell, str):
        return None
    return cell.strip() or None


def _day(value: object) -> date | None:
    """A date as the export writes it: «22.09.2026 9:38:50», or a real date cell."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value) if isinstance(value, str) else None
    if text is None:
        return None
    match = _DATE_TEXT.match(text)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def read_export_header(rows: list[tuple[Cell, ...]]) -> ExportHeader:
    """«Контрагент: …», «Дата начала: …» and the like, wherever they sit in the block."""
    values: dict[str, str] = {}
    for cells in rows:
        for cell in cells:
            text = _text(cell)
            if text is None:
                continue
            match = _HEADER_VALUE.match(text)
            if match is not None:
                values.setdefault(match.group(1).strip().lower(), match.group(2))
    start, end = _day(values.get("дата начала")), _day(values.get("дата окончания"))
    period = Period(start, end) if start is not None and end is not None and start <= end else None
    return ExportHeader(
        counterparty=values.get("контрагент"),
        organization=values.get("организация"),
        period=period,
    )


def find_table(rows: list[tuple[Cell, ...]]) -> int | None:
    """Index of the row that holds the column titles; None if the table is not there.

    The print starts with a parameters block, so the titles are not on the first row.
    A row counts as the header when it names both the date and the receipts column.
    """
    for index, cells in enumerate(rows[:MAX_HEADER_ROWS]):
        titles = {(_text(cell) or "").lower() for cell in cells}
        if DATE_TITLE in titles and INCOMING_TITLE in titles:
            return index
    return None


def payment_id(document: Cell) -> Cell:
    """The document's own number («00БП-036835»), or the whole line if there is none."""
    text = _text(document)
    if text is None:
        return None
    match = _DOCUMENT_NUMBER.search(text)
    if match is not None:
        return match.group()
    return text[:100]


def _is_totals(cells: tuple[Cell, ...]) -> bool:
    return any((_text(cell) or "").lower() == TOTALS_WORD for cell in cells)


def convert_payments(rows: list[tuple[Cell, ...]], inn: str) -> ConvertedSheet:
    """Turn the 1C print of one counterparty's payments into the «Платежи» sheet.

    ``inn`` is whose file this is: the export does not say, the dialog does. Rows after
    «Итого» are the print's own totals and are left out; an outgoing payment is reported
    and skipped, because the contract accepts receipts only.
    """
    issues: list[ImportIssue] = []
    header_index = find_table(rows)
    export = read_export_header(rows[:header_index] if header_index else rows[:MAX_HEADER_ROWS])
    if header_index is None:
        issues.append(
            issue(
                PAYMENTS_SHEET,
                "export_table_missing",
                IssueSeverity.ERROR,
                "В файле не найдена таблица с колонками «Дата» и «Поступление».",
            )
        )
        return ConvertedSheet((), (), export, tuple(issues))

    titles = {(_text(cell) or "").lower(): index for index, cell in enumerate(rows[header_index])}
    date_at = titles[DATE_TITLE]
    incoming_at = titles[INCOMING_TITLE]
    document_at = titles.get(DOCUMENT_TITLE)
    outgoing_at = titles.get(OUTGOING_TITLE)

    def cell_of(cells: tuple[Cell, ...], index: int | None) -> Cell:
        return cells[index] if index is not None and index < len(cells) else None

    converted: list[tuple[int, tuple[Cell, ...]]] = []
    for offset, cells in enumerate(rows[header_index + 1 :]):
        row_number = header_index + 2 + offset
        if not any(_text(cell) or cell not in (None, "") for cell in cells):
            continue
        if _is_totals(cells):
            break  # the print's own totals close the table
        incoming = cell_of(cells, incoming_at)
        outgoing = cell_of(cells, outgoing_at)
        if incoming in (None, "", 0) and outgoing not in (None, "", 0):
            issues.append(
                issue(
                    PAYMENTS_SHEET,
                    "payment_is_outgoing",
                    IssueSeverity.WARNING,
                    "Строка со списанием пропущена: учитываются только поступления.",
                    row_number,
                )
            )
            continue
        raw_date = cell_of(cells, date_at)
        day = _day(raw_date)
        converted.append(
            (
                row_number,
                (
                    inn,
                    payment_id(cell_of(cells, document_at)),
                    day if day is not None else raw_date,
                    incoming,
                ),
            )
        )
    return ConvertedSheet(PAYMENT_TITLES, tuple(converted), export, tuple(issues))
