"""S2-04: pure parsing of the mandatory «Контрагенты» sheet into the data contract.

No network, filesystem or Excel library here: the reader in infrastructure turns a
workbook into plain cells, and this module applies the contract rules. Problems are
reported as ``domain.imports.ImportIssue`` — one list with a stable code, a severity
and a coordinate; a reason never echoes the raw INN, which may hold private data.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.inn import InvalidInn, validate_legal_inn

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


class FormulaCell:
    """Marker for a source cell holding a formula; values must replace formulas."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "FormulaCell()"


# A raw cell as produced by the sheet reader before contract normalization.
Cell = str | int | float | Decimal | date | datetime | bool | FormulaCell | None


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


class _CellError(Exception):
    """Internal: one cell failed a contract rule; carries a stable code and safe reason."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


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


def _column_letter(index: int) -> str:
    """1-based column number to its Excel letter (1 -> A, 27 -> AA)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _is_blank(cell: Cell) -> bool:
    if isinstance(cell, FormulaCell):
        return False
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _reject_formula(cell: Cell) -> None:
    if isinstance(cell, FormulaCell):
        raise _CellError(
            "formula_forbidden", "Замените формулу на значение; формулы не вычисляются."
        )


def _digits_from_number(value: int | float | Decimal) -> str:
    """Exact integer recovery only; lost digits or fractions are never guessed."""
    dec = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not dec.is_finite() or dec != dec.to_integral_value():
        raise _CellError("inn_invalid", "ИНН должен быть целым числом без дробной части.")
    return str(int(dec))


def _inn(cell: Cell) -> str:
    _reject_formula(cell)
    if isinstance(cell, bool):
        raise _CellError("inn_invalid", "ИНН должен быть числом или строкой из цифр.")
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        raise _CellError("inn_missing", "ИНН обязателен.")
    if isinstance(cell, str):
        text = cell.strip()
    elif isinstance(cell, (int, float, Decimal)):
        text = _digits_from_number(cell)
    else:
        raise _CellError("inn_invalid", "ИНН должен быть числом или строкой из цифр.")
    try:
        return validate_legal_inn(text)
    except InvalidInn as error:
        # The validator message is already safe and never repeats the raw value.
        raise _CellError("inn_invalid", str(error)) from None


def _name(cell: Cell) -> str | None:
    _reject_formula(cell)
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell.strip() or None
    raise _CellError("name_invalid", "Наименование должно быть текстом.")


def _money(cell: Cell) -> Decimal | None:
    _reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None
    if isinstance(cell, bool):
        raise _CellError("debt_not_number", "Сумма долга должна быть числом.")
    if isinstance(cell, int):
        dec = Decimal(cell)
    elif isinstance(cell, float):
        dec = Decimal(str(cell))
    elif isinstance(cell, Decimal):
        dec = cell
    elif isinstance(cell, str):
        text = cell.strip().replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            dec = Decimal(text)
        except InvalidOperation:
            raise _CellError("debt_not_number", "Сумма долга не распознана как число.") from None
    else:
        raise _CellError("debt_not_number", "Сумма долга должна быть числом.")
    if not dec.is_finite():
        raise _CellError("debt_not_finite", "Сумма долга должна быть конечным числом.")
    if dec < 0:
        raise _CellError("debt_negative", "Сумма долга не может быть отрицательной.")
    if dec.as_tuple().exponent < -2:
        raise _CellError("debt_precision", "Сумма долга указывается с точностью до копеек.")
    return dec


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


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RU_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _parse_date(cell: Cell) -> date | None:
    _reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None
    if isinstance(cell, bool):
        raise _CellError("date_invalid", "Дата не распознана.")
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    if isinstance(cell, str):
        text = cell.strip()
        if _ISO_DATE.fullmatch(text):
            try:
                return date.fromisoformat(text)
            except ValueError:
                raise _CellError("date_invalid", "Дата не распознана.") from None
        match = _RU_DATE.fullmatch(text)
        if match:
            day, month, year = (int(part) for part in match.groups())
            try:
                return date(year, month, day)
            except ValueError:
                raise _CellError("date_invalid", "Дата не распознана.") from None
        # Anything ambiguous (e.g. 01/02/2026) is rejected rather than guessed.
        raise _CellError("date_invalid", "Дата должна быть в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")
    raise _CellError("date_invalid", "Дата не распознана.")


_NORMALIZERS = {
    "name": _name,
    "cutoff_date": _parse_date,
    "debt": _money,
    "overdue_days": _overdue_days,
    "last_payment_date": _parse_date,
}


def _map_header(header: tuple[Cell, ...], issues: list[ImportIssue]) -> dict[str, int] | None:
    """Map contract fields to column indexes, or return None if the header is unusable."""
    fields: dict[str, int] = {}
    fatal = False
    for index, cell in enumerate(header):
        column = _column_letter(index + 1)
        if isinstance(cell, FormulaCell):
            issues.append(
                _issue(
                    "header_formula",
                    IssueSeverity.ERROR,
                    "Заголовок не может быть формулой.",
                    1,
                    column,
                )
            )
            fatal = True
            continue
        title = cell.strip() if isinstance(cell, str) else ("" if cell is None else str(cell))
        if not title:
            continue
        if title not in _FIELDS:
            issues.append(
                _issue(
                    "unknown_column",
                    IssueSeverity.WARNING,
                    f"Неизвестная колонка «{title}» игнорируется.",
                    1,
                    column,
                )
            )
            continue
        field = _FIELDS[title]
        if field in fields:
            issues.append(
                _issue(
                    "duplicate_column",
                    IssueSeverity.ERROR,
                    f"Колонка «{title}» указана несколько раз.",
                    1,
                    column,
                )
            )
            fatal = True
            continue
        fields[field] = index
    if "inn" not in fields:
        issues.append(
            _issue(
                "inn_column_missing", IssueSeverity.ERROR, "Обязательная колонка «ИНН» отсутствует."
            )
        )
        return None
    return None if fatal else fields


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
