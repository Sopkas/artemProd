"""S8-08: the INNs of a file sent for monitoring.

Two kinds of file come, and both are read as they are:

- **The 1C report** «Отчет по просроченным лизинговым платежам» in its variant with the
  «ИНН» column (the customer's «образец», 16.10.2026: 318 companies, all with an INN). It
  is read by the report's own parser (S7-01), and the name comes with the INN. A company
  of the report without an INN — the other variant prints none — cannot be watched and is
  counted, not guessed by its name.
- **Any table with an «ИНН» column**, its header in one of the first rows: a list the
  customer keeps himself. A column of names next to it («Наименование», «Контрагент»…)
  gives the names; a table of INNs alone with no header at all is taken as one too.

Wrong values are named by their row; the value itself is never repeated.
"""

from dataclasses import dataclass

from claims_assistant.domain.debt_report import read_debt_report
from claims_assistant.domain.inn import InnProblem, InvalidInn, validate_inn
from claims_assistant.domain.sheet_rules import Cell, CellError, parse_inn
from claims_assistant.domain.watchlist import Candidate, Invalid, unique

from .contract_link import merge_repeats
from .imports import ImportLimits, OutlineReader, RawSheetReader

HEADER_ROWS = 20
INN_TITLES = ("инн", "инн контрагента", "инн организации")
NAME_TITLES = (
    "наименование",
    "название",
    "контрагент",
    "организация",
    "краткое наименование",
    "полное наименование",
)


@dataclass(frozen=True, slots=True)
class FileList:
    candidates: tuple[Candidate, ...] = ()
    invalid: tuple[Invalid, ...] = ()
    repeats: int = 0
    without_inn: int = 0  # companies of a report that prints no INN for them
    # Why nothing could be taken, in the user's words; None when something was.
    problem: str | None = None


NO_INN_COLUMN = (
    "В файле не нашлось колонки «ИНН». Пришлите отчёт из 1С с колонкой ИНН, таблицу, где "
    "одна из колонок называется «ИНН», или ИНН сообщением."
)
REPORT_WITHOUT_INN = (
    "В отчёте нет ИНН организаций, а по названию мониторинг не ведётся. Пришлите вариант "
    "отчёта с колонкой «ИНН» или ИНН сообщением."
)


def _title(cell: Cell) -> str:
    return " ".join(str(cell).split()).lower() if isinstance(cell, str) else ""


def _name(cell: Cell) -> str | None:
    if not isinstance(cell, str):
        return None
    text = " ".join(cell.split())
    return text[:300] or None


def _from_report(rows) -> FileList | None:
    """The 1C report, if this is one; None when the file is something else."""
    report = read_debt_report(list(rows))
    if not report.counterparties:
        return None
    counterparties, _repeats = merge_repeats(report.counterparties)
    found = [Candidate(c.inn, _name(c.name)) for c in counterparties if c.inn]
    without = sum(1 for c in counterparties if not c.inn)
    if not found:
        return FileList(without_inn=without, problem=REPORT_WITHOUT_INN)
    candidates, repeats = unique(found)
    return FileList(candidates, repeats=repeats, without_inn=without)


def _header(rows: tuple[tuple[Cell, ...], ...]) -> tuple[int, int, int | None] | None:
    """(header row, INN column, name column or None) of the first row naming an INN."""
    for number, cells in enumerate(rows[:HEADER_ROWS]):
        titles = [_title(cell) for cell in cells]
        inn = next((i for i, title in enumerate(titles) if title in INN_TITLES), None)
        if inn is None:
            continue
        name = next((i for title in NAME_TITLES for i, t in enumerate(titles) if t == title), None)
        return number, inn, name
    return None


def _digits(cell: Cell) -> str | None:
    """The digits of a cell that holds a whole number or a string of digits, else None."""
    if isinstance(cell, bool):
        return None
    if isinstance(cell, int | float) and cell == int(cell):
        return str(int(cell))
    if isinstance(cell, str) and cell.strip().isdigit():
        return cell.strip()
    return None


def _valid(cell: Cell) -> bool:
    digits = _digits(cell)
    try:
        return digits is not None and validate_inn(digits) == digits
    except InvalidInn:
        return False


def _inn_column(rows: tuple[tuple[Cell, ...], ...]) -> int | None:
    """Without a header: the column holding the most valid INNs (the checksum makes a date
    or an amount passing for one all but impossible), or None when no column holds any."""
    counts: dict[int, int] = {}
    for cells in rows:
        for index, cell in enumerate(cells):
            if _valid(cell):
                counts[index] = counts.get(index, 0) + 1
    return max(counts, key=lambda i: (counts[i], -i)) if counts else None


def _from_table(rows: tuple[tuple[Cell, ...], ...]) -> FileList:
    header = _header(rows)
    if header is not None:
        start, inn_column, name_column = header
    else:
        # «тпз 3» (16.10.2026): the report with the header cut off — the INN on a
        # company's row, a date in the same column on its contracts' rows below.
        inn_column = _inn_column(rows)
        if inn_column is None:
            return FileList(problem=NO_INN_COLUMN)
        start, name_column = -1, None
    found, invalid = [], []
    for number, cells in enumerate(rows[start + 1 :], start=start + 2):
        cell = cells[inn_column] if inn_column < len(cells) else None
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            continue  # a blank line or a subtotal without an INN
        if header is None:
            digits = _digits(cell)
            if digits is None or len(digits) not in (9, 10, 11, 12):
                continue  # without a header only what looks like an INN is read
        try:
            inn = parse_inn(cell)
        except CellError:
            invalid.append(Invalid(number, _problem(cell)))
            continue
        name = None
        if name_column is not None and name_column < len(cells):
            name = _name(cells[name_column])
        found.append(Candidate(inn, name))
    candidates, repeats = unique(found)
    return FileList(candidates, tuple(invalid), repeats)


def _problem(cell: Cell) -> InnProblem:
    """What is wrong with a cell, in the validator's own terms."""
    whole = isinstance(cell, int | float) and not isinstance(cell, bool) and cell == int(cell)
    try:
        validate_inn(str(int(cell)) if whole else str(cell))
    except InvalidInn as error:
        return error.problem
    return InnProblem.NOT_DIGITS  # a formula, a fraction: a cell that is not a value


def read_watch_file(
    data: bytes, reader: OutlineReader | RawSheetReader, limits: ImportLimits = ImportLimits()
) -> FileList:
    """The INNs of a file sent for monitoring; a reader error propagates (WorkbookError)."""
    if isinstance(reader, OutlineReader):
        report = _from_report(reader.read_outline(data, limits))
        if report is not None:
            return report
    return _from_table(reader.read_rows(data, limits))
