"""S7-01: the customer's «Отчет по просроченным лизинговым платежам», as it is printed.

This is the report the claims specialist already works from, and the only thing in her
work that is automated today. It is a 1C print, not a table: one column holds three
levels of a tree (sales point → counterparty → contract), the rest of the eighty columns
carry the values of whichever level the row belongs to.

Two things decide how it is read.

**The level comes from the indent, not from the name.** «Департамент регионального
развития» is a sales point and «Данилов Максим Владимирович, ИП» is a counterparty; no
rule over names would tell them apart. 1C indents each level, and the step differs
between exports (2/4 in one, 3/6 in another), so the indents are ranked, never compared
with a constant.

**Every contract keeps its own overdue.** The customer was explicit (23.09.2026): a
client may owe on one contract for 307 days and on another for 30, and the specialist
works with the contract, not with a sum. So contracts are kept apart; the counterparty
level carries the total and the worst of them, exactly as the report's own subtotal does.

The report says nothing about the date its numbers are true on — the title ends with «на
дату» and nothing follows. The analysis date comes from the dialog, where the user names
it anyway.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.sheet_rules import (
    Cell,
    CellError,
    FormulaCell,
    issue,
    parse_amount,
    parse_date,
    parse_inn,
)

SHEET_NAME = "Отчет по ТПЗ"
MAX_HEADER_ROWS = 20
_SKIP_PREFIXES = ("отбор", "итого", "отчет")
# Titles of the legend rows themselves: they name the levels, they are not data.
_LEVEL_TITLES = {"точка продаж", "контрагент", "договор лизинга"}

# Column titles we read; everything else in the print is ignored.
TITLES = {
    "инн": "inn",
    "сумма просроченной задолженности с вычетом н/р дней": "overdue",
    "сумма": "amount",
    "дней": "days",
    "срок действия до": "due_until",
    "предмет лизинга": "subject",
    "описание": "note",
}


@dataclass(frozen=True, slots=True)
class ContractDebt:
    """One leasing contract of one counterparty, with its own overdue and its own days."""

    name: str
    overdue: Decimal | None = None
    days: int | None = None
    due_until: date | None = None
    subject: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class CounterpartyDebt:
    name: str
    inn: str | None = None
    group: str | None = None  # the sales point the report groups this counterparty under
    contracts: tuple[ContractDebt, ...] = ()
    note: str | None = None

    @property
    def total_overdue(self) -> Decimal | None:
        """The sum over contracts; None when not a single contract says an amount."""
        amounts = [c.overdue for c in self.contracts if c.overdue is not None]
        return sum(amounts, Decimal(0)) if amounts else None

    @property
    def worst_days(self) -> int | None:
        """The longest overdue among the contracts — what the report's own subtotal shows."""
        days = [c.days for c in self.contracts if c.days is not None]
        return max(days) if days else None


@dataclass(frozen=True, slots=True)
class DebtReport:
    counterparties: tuple[CounterpartyDebt, ...] = ()
    issues: tuple[ImportIssue, ...] = ()

    @property
    def contracts(self) -> int:
        return sum(len(row.contracts) for row in self.counterparties)


def _text(cell: Cell, limit: int = 500) -> str | None:
    if isinstance(cell, FormulaCell) or not isinstance(cell, str):
        return None
    return cell.strip()[:limit] or None


def _skip(title: str) -> bool:
    lowered = title.lower()
    return lowered in _LEVEL_TITLES or lowered.startswith(_SKIP_PREFIXES)


def map_columns(rows: list[tuple[int, tuple[Cell, ...]]]) -> dict[str, int]:
    """Column indexes of the values, taken from the legend rows above the data.

    The legend is spread over the three rows that name the levels; each of them also
    names the columns of its own level, so all of them are read.
    """
    columns: dict[str, int] = {}
    for _indent, cells in rows[:MAX_HEADER_ROWS]:
        for index, cell in enumerate(cells):
            title = (_text(cell) or "").lower()
            field = TITLES.get(title)
            if field is not None:
                columns.setdefault(field, index)
    return columns


def _levels(rows: list[tuple[int, tuple[Cell, ...]]]) -> tuple[int | None, int | None]:
    """The indents that mean «contract» and «counterparty» in this particular file."""
    indents = sorted(
        {
            indent
            for indent, cells in rows
            if cells and (title := _text(cells[0])) and not _skip(title)
        }
    )
    if not indents:
        return None, None
    contract = indents[-1]
    counterparty = indents[-2] if len(indents) > 1 else None
    return contract, counterparty


def _value(cells: tuple[Cell, ...], columns: dict[str, int], field: str) -> Cell:
    index = columns.get(field)
    return cells[index] if index is not None and index < len(cells) else None


def _overdue(cells, columns, row_number, issues) -> Decimal | None:
    """«Сумма просроченной задолженности», or «Сумма» when the report has only that one.

    The two are equal in the customer's report, but a contract with **zero** overdue must
    keep its zero: falling back on emptiness alone once turned a paid-up contract into the
    whole «Сумма» and inflated the counterparty's debt fivefold (found by A on #53).
    """
    for field in ("overdue", "amount"):
        if columns.get(field) is None:
            continue
        if _value(cells, columns, field) in (None, ""):
            continue
        return _amount(cells, columns, field, row_number, issues)
    return None


def _amount(cells, columns, field, row_number, issues) -> Decimal | None:
    try:
        return parse_amount(
            _value(cells, columns, field), prefix="overdue", label="Сумма просрочки"
        )
    except CellError as error:
        issues.append(
            issue(SHEET_NAME, error.code, IssueSeverity.WARNING, error.reason, row_number)
        )
        return None


def read_debt_report(rows: list[tuple[int, tuple[Cell, ...]]]) -> DebtReport:
    """Read the print into counterparties with their contracts; never guess a level."""
    issues: list[ImportIssue] = []
    columns = map_columns(rows)
    contract_level, counterparty_level = _levels(rows)
    if contract_level is None or counterparty_level is None or "days" not in columns:
        issues.append(
            issue(
                SHEET_NAME,
                "report_not_recognised",
                IssueSeverity.ERROR,
                "Это не похоже на отчёт по просроченным платежам: "
                "не найдены уровни отчёта или колонка «Дней».",
            )
        )
        return DebtReport(issues=tuple(issues))

    counterparties: list[CounterpartyDebt] = []
    contracts: list[ContractDebt] = []
    current: CounterpartyDebt | None = None
    group: str | None = None

    def close() -> None:
        nonlocal current, contracts
        if current is not None:
            counterparties.append(
                CounterpartyDebt(
                    name=current.name,
                    inn=current.inn,
                    group=current.group,
                    contracts=tuple(contracts),
                    note=current.note,
                )
            )
        current, contracts = None, []

    for row_number, (indent, cells) in enumerate(rows, start=1):
        title = _text(cells[0]) if cells else None
        if title is None or _skip(title):
            continue
        if indent == contract_level:
            if current is None:
                issues.append(
                    issue(
                        SHEET_NAME,
                        "contract_without_counterparty",
                        IssueSeverity.ERROR,
                        "Договор в отчёте не привязан к контрагенту; строка пропущена.",
                        row_number,
                    )
                )
                continue
            days = _value(cells, columns, "days")
            contracts.append(
                ContractDebt(
                    name=title,
                    overdue=_overdue(cells, columns, row_number, issues),
                    days=int(days)
                    if isinstance(days, int) and not isinstance(days, bool)
                    else None,
                    due_until=_safe_date(cells, columns, row_number, issues),
                    subject=_text(_value(cells, columns, "subject")),
                    note=_text(_value(cells, columns, "note")),
                )
            )
        elif indent == counterparty_level:
            close()
            current = CounterpartyDebt(
                name=title,
                inn=_safe_inn(cells, columns, row_number, issues),
                group=group,
                note=_text(_value(cells, columns, "note")),
            )
        else:
            close()
            group = title  # a shallower level groups the counterparties (the sales point)
    close()
    return DebtReport(tuple(counterparties), tuple(issues))


def _safe_date(cells, columns, row_number, issues) -> date | None:
    try:
        return parse_date(_value(cells, columns, "due_until"))
    except CellError as error:
        issues.append(
            issue(SHEET_NAME, error.code, IssueSeverity.WARNING, error.reason, row_number)
        )
        return None


def _safe_inn(cells, columns, row_number, issues) -> str | None:
    """The INN of the counterparty; the column appears only in one variant of the report."""
    raw = _value(cells, columns, "inn")
    if raw in (None, ""):
        return None
    try:
        return parse_inn(raw)
    except CellError as error:
        issues.append(issue(SHEET_NAME, error.code, IssueSeverity.ERROR, error.reason, row_number))
        return None
