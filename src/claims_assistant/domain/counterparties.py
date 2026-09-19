"""S2-04: pure parsing of the mandatory «Контрагенты» sheet into the data contract.

No network, filesystem or Excel library here: the reader in infrastructure turns a
workbook into plain cells, and this module applies the contract rules. Problems are
reported as ``domain.imports.ImportIssue`` — one list with a stable code, a severity
and a coordinate; a reason never echoes the raw INN, which may hold private data.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.sheet_rules import (
    Cell,
    FormulaCell,
    map_header,
    parse_amount,
)
from claims_assistant.domain.sheet_rules import CellError as _CellError
from claims_assistant.domain.sheet_rules import column_letter as _column_letter
from claims_assistant.domain.sheet_rules import is_blank as _is_blank
from claims_assistant.domain.sheet_rules import parse_date as _parse_date
from claims_assistant.domain.sheet_rules import parse_inn as _inn
from claims_assistant.domain.sheet_rules import reject_formula as _reject_formula

__all__ = [
    "COLUMN_TITLES",
    "SHEET_NAME",
    "Cell",
    "CounterpartiesImport",
    "CounterpartyRow",
    "FormulaCell",
    "ImportLimits",
    "parse_counterparties",
]

SHEET_NAME = "Контрагенты"

# Fixed Russian headers in output order; only «ИНН» is mandatory.
COLUMN_TITLES = (
    "ИНН",
    "Наименование",
    "Дата среза",
    "Сумма долга",
    "Дней просрочки",
    "Дата последнего платежа",
)
_FIELDS = {
    "ИНН": "inn",
    "Наименование": "name",
    "Дата среза": "cutoff_date",
    "Сумма долга": "debt",
    "Дней просрочки": "overdue_days",
    "Дата последнего платежа": "last_payment_date",
}


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_rows: int = 10_000
    max_unpacked_bytes: int = 50 * 1024 * 1024

    def __post_init__(self) -> None:
        for limit in (self.max_rows, self.max_unpacked_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("Import limits must be positive integers")


@dataclass(frozen=True, slots=True)
class CounterpartyRow:
    inn: str
    name: str | None = None
    cutoff_date: date | None = None
    debt: Decimal | None = None
    overdue_days: int | None = None
    last_payment_date: date | None = None

    def __post_init__(self) -> None:
        if self.debt is not None and (not self.debt.is_finite() or self.debt < 0):
            raise ValueError("Debt must be a finite, non-negative Decimal")
        if self.overdue_days is not None and self.overdue_days < 0:
            raise ValueError("Overdue days must be non-negative")


@dataclass(frozen=True, slots=True)
class CounterpartiesImport:
    rows: tuple[CounterpartyRow, ...] = ()
    issues: tuple[ImportIssue, ...] = ()

    @property
    def has_usable_rows(self) -> bool:
        # No usable rows (or any error) means the external check must not be launched.
        return bool(self.rows) and not any(
            issue.severity is IssueSeverity.ERROR for issue in self.issues
        )


def _issue(
    code: str,
    severity: IssueSeverity,
    reason: str,
    row: int | None = None,
    column: str | None = None,
) -> ImportIssue:
    return ImportIssue(
        code=code, severity=severity, sheet=SHEET_NAME, reason=reason, row=row, column=column
    )


def _name(cell: Cell) -> str | None:
    _reject_formula(cell)
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell.strip() or None
    raise _CellError("name_invalid", "Наименование должно быть текстом.")


def _money(cell: Cell) -> Decimal | None:
    return parse_amount(cell, prefix="debt", label="Сумма долга")


def _map_header(header: tuple[Cell, ...], issues: list[ImportIssue]) -> dict[str, int] | None:
    """Map contract fields to column indexes, or return None if the header is unusable."""
    return map_header(header, _FIELDS, frozenset({"inn"}), SHEET_NAME, issues)


def _overdue_days(cell: Cell) -> int | None:
    _reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None
    if isinstance(cell, bool):
        raise _CellError("overdue_not_integer", "Дней просрочки должно быть целым числом.")
    if isinstance(cell, int):
        value = cell
    elif isinstance(cell, (float, Decimal)):
        dec = Decimal(str(cell)) if isinstance(cell, float) else cell
        if not dec.is_finite() or dec != dec.to_integral_value():
            raise _CellError("overdue_not_integer", "Дней просрочки должно быть целым числом.")
        value = int(dec)
    elif isinstance(cell, str):
        text = cell.strip()
        if not text.lstrip("-").isdigit():
            raise _CellError("overdue_not_integer", "Дней просрочки должно быть целым числом.")
        value = int(text)
    else:
        raise _CellError("overdue_not_integer", "Дней просрочки должно быть целым числом.")
    if value < 0:
        raise _CellError("overdue_negative", "Дней просрочки не может быть отрицательным.")
    return value


_NORMALIZERS = {
    "name": _name,
    "cutoff_date": _parse_date,
    "debt": _money,
    "overdue_days": _overdue_days,
    "last_payment_date": _parse_date,
}


def _parse_row(
    row_number: int,
    cells: tuple[Cell, ...],
    fields: dict[str, int],
    analysis_date: date | None,
) -> tuple[list[ImportIssue], CounterpartyRow | None]:
    def cell_at(field: str) -> Cell:
        index = fields.get(field)
        return cells[index] if index is not None and index < len(cells) else None

    def column(field: str) -> str:
        return _column_letter(fields[field] + 1)

    issues: list[ImportIssue] = []
    try:
        inn = _inn(cell_at("inn"))
    except _CellError as error:
        # Without a valid INN the row cannot be matched, so it is dropped.
        return [
            _issue(error.code, IssueSeverity.ERROR, error.reason, row_number, column("inn"))
        ], None

    values: dict[str, object] = {}
    for field, normalize in _NORMALIZERS.items():
        if field not in fields:
            values[field] = None
            continue
        try:
            values[field] = normalize(cell_at(field))
        except _CellError as error:
            issues.append(
                _issue(error.code, IssueSeverity.ERROR, error.reason, row_number, column(field))
            )

    if values.get("debt") is not None or values.get("overdue_days") is not None:
        if values.get("cutoff_date") is None:
            issues.append(
                _issue(
                    "cutoff_required",
                    IssueSeverity.ERROR,
                    "Дата среза обязательна при наличии долга или просрочки.",
                    row_number,
                )
            )
    if analysis_date is not None:
        cutoff = values.get("cutoff_date")
        if cutoff is not None and cutoff != analysis_date:
            issues.append(
                _issue(
                    "cutoff_mismatch",
                    IssueSeverity.ERROR,
                    "Дата среза должна совпадать с датой анализа.",
                    row_number,
                    column("cutoff_date"),
                )
            )
        last_payment = values.get("last_payment_date")
        if last_payment is not None and last_payment > analysis_date:
            issues.append(
                _issue(
                    "last_payment_future",
                    IssueSeverity.ERROR,
                    "Дата последнего платежа не может быть позже даты анализа.",
                    row_number,
                    column("last_payment_date"),
                )
            )

    if issues:
        return issues, None
    return [], CounterpartyRow(
        inn=inn,
        name=values["name"],
        cutoff_date=values["cutoff_date"],
        debt=values["debt"],
        overdue_days=values["overdue_days"],
        last_payment_date=values["last_payment_date"],
    )


def parse_counterparties(
    header: tuple[Cell, ...],
    rows: Iterable[tuple[int, tuple[Cell, ...]]],
    limits: ImportLimits = ImportLimits(),
    analysis_date: date | None = None,
) -> CounterpartiesImport:
    """Validate the «Контрагенты» sheet and return normalized rows plus one issue list."""
    issues: list[ImportIssue] = []
    fields = _map_header(header, issues)
    if fields is None:
        return CounterpartiesImport(issues=tuple(issues))

    by_inn: dict[str, tuple[int, CounterpartyRow]] = {}
    order: list[str] = []
    seen = 0
    for row_number, cells in rows:
        seen += 1
        if seen > limits.max_rows:
            issues.append(
                _issue(
                    "row_limit",
                    IssueSeverity.ERROR,
                    f"Превышен лимит строк ({limits.max_rows}).",
                    row_number,
                )
            )
            break
        if all(_is_blank(cell) for cell in cells):
            continue
        row_issues, parsed = _parse_row(row_number, cells, fields, analysis_date)
        if parsed is None:
            issues.extend(row_issues)
            continue
        existing = by_inn.get(parsed.inn)
        if existing is None:
            by_inn[parsed.inn] = (row_number, parsed)
            order.append(parsed.inn)
        elif existing[1] == parsed:
            issues.append(
                _issue(
                    "duplicate_row",
                    IssueSeverity.WARNING,
                    f"Строка полностью дублирует строку {existing[0]} и объединена.",
                    row_number,
                )
            )
        else:
            issues.append(
                _issue(
                    "inn_conflict",
                    IssueSeverity.ERROR,
                    f"Конфликт данных по ИНН со строкой {existing[0]}; требуется исправление.",
                    row_number,
                    _column_letter(fields["inn"] + 1),
                )
            )

    return CounterpartiesImport(
        rows=tuple(by_inn[inn][1] for inn in order),
        issues=tuple(issues),
    )
