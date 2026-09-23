"""Cell and header rules shared by every input sheet (docs/data-contracts.md, «Общие правила»).

The reader in infrastructure turns a workbook into plain cells; each sheet parser applies its
own contract on top of these rules. A rule that fails raises ``CellError`` with a stable
code and a reason that never repeats the cell contents (an INN or amount may be private).
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.inn import InvalidInn, validate_inn


class FormulaCell:
    """Marker for a source cell holding a formula; values must replace formulas."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "FormulaCell()"


# A raw cell as produced by the sheet reader before contract normalization.
Cell = str | int | float | Decimal | date | datetime | bool | FormulaCell | None

# Thousands separators seen in Excel and 1C exports: space, no-break and narrow spaces.
_GROUP_SEPARATORS = str.maketrans(
    "",
    "",
    " " + chr(0x00A0) + chr(0x202F) + chr(0x2009),  # written as codes: invisible in source
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RU_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def number_text(text: str) -> str:
    """Drop group separators and turn a decimal comma into a point: «1 500,25» → «1500.25»."""
    return text.strip().translate(_GROUP_SEPARATORS).replace(",", ".")


class CellError(Exception):
    """One cell failed a contract rule; carries a stable code and a safe reason."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def issue(
    sheet: str,
    code: str,
    severity: IssueSeverity,
    reason: str,
    row: int | None = None,
    column: str | None = None,
) -> ImportIssue:
    return ImportIssue(
        code=code, severity=severity, sheet=sheet, reason=reason, row=row, column=column
    )


def column_letter(index: int) -> str:
    """1-based column number to its Excel letter (1 -> A, 27 -> AA)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def is_blank(cell: Cell) -> bool:
    if isinstance(cell, FormulaCell):
        return False
    return cell is None or (isinstance(cell, str) and not cell.strip())


def reject_formula(cell: Cell) -> None:
    if isinstance(cell, FormulaCell):
        raise CellError(
            "formula_forbidden", "Замените формулу на значение; формулы не вычисляются."
        )


def _digits_from_number(value: int | float | Decimal) -> str:
    """Exact integer recovery only; lost digits or fractions are never guessed."""
    dec = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not dec.is_finite() or dec != dec.to_integral_value():
        raise CellError("inn_invalid", "ИНН должен быть целым числом без дробной части.")
    return str(int(dec))


def parse_inn(cell: Cell) -> str:
    reject_formula(cell)
    if isinstance(cell, bool):
        raise CellError("inn_invalid", "ИНН должен быть числом или строкой из цифр.")
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        raise CellError("inn_missing", "ИНН обязателен.")
    if isinstance(cell, str):
        text = cell.strip()
    elif isinstance(cell, (int, float, Decimal)):
        text = _digits_from_number(cell)
    else:
        raise CellError("inn_invalid", "ИНН должен быть числом или строкой из цифр.")
    try:
        return validate_inn(text)
    except InvalidInn as error:
        # The validator message is already safe and never repeats the raw value.
        raise CellError("inn_invalid", str(error)) from None


def parse_date(cell: Cell) -> date | None:
    """Excel date or ДД.ММ.ГГГГ / ГГГГ-ММ-ДД; ambiguous forms are rejected, not guessed."""
    reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None
    if isinstance(cell, bool):
        raise CellError("date_invalid", "Дата не распознана.")
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
                raise CellError("date_invalid", "Дата не распознана.") from None
        match = _RU_DATE.fullmatch(text)
        if match:
            day, month, year = (int(part) for part in match.groups())
            try:
                return date(year, month, day)
            except ValueError:
                raise CellError("date_invalid", "Дата не распознана.") from None
        raise CellError("date_invalid", "Дата должна быть в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")
    raise CellError("date_invalid", "Дата не распознана.")


def parse_amount(cell: Cell, *, prefix: str, label: str, positive: bool = False) -> Decimal | None:
    """Rubles to the kopeck; a decimal comma and group separators are accepted.

    ``label`` is a feminine noun phrase («Сумма долга») used in the reasons; codes are
    ``<prefix>_not_number``, ``_not_finite``, ``_negative``, ``_zero``, ``_precision``.
    """
    reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None
    if isinstance(cell, bool):
        raise CellError(f"{prefix}_not_number", f"{label} должна быть числом.")
    if isinstance(cell, int):
        dec = Decimal(cell)
    elif isinstance(cell, float):
        dec = Decimal(str(cell))
    elif isinstance(cell, Decimal):
        dec = cell
    elif isinstance(cell, str):
        text = number_text(cell)
        try:
            dec = Decimal(text)
        except InvalidOperation:
            raise CellError(f"{prefix}_not_number", f"{label} не распознана как число.") from None
    else:
        raise CellError(f"{prefix}_not_number", f"{label} должна быть числом.")
    if not dec.is_finite():
        raise CellError(f"{prefix}_not_finite", f"{label} должна быть конечным числом.")
    if dec < 0:
        raise CellError(f"{prefix}_negative", f"{label} не может быть отрицательной.")
    if positive and dec == 0:
        raise CellError(f"{prefix}_zero", f"{label} должна быть больше нуля.")
    if dec.as_tuple().exponent < -2:
        raise CellError(f"{prefix}_precision", f"{label} указывается с точностью до копеек.")
    return dec


def parse_record_id(cell: Cell, *, prefix: str, label: str) -> str:
    """A required operation ID: text, or an integer number exactly as written."""
    reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        raise CellError(f"{prefix}_missing", f"{label} обязателен.")
    if isinstance(cell, str):
        text = cell.strip()
        if len(text) > 100:
            raise CellError(f"{prefix}_invalid", f"{label} длиннее 100 символов.")
        return text
    if isinstance(cell, bool) or not isinstance(cell, (int, float, Decimal)):
        raise CellError(f"{prefix}_invalid", f"{label} должен быть текстом или целым числом.")
    dec = Decimal(str(cell)) if isinstance(cell, float) else Decimal(cell)
    if not dec.is_finite() or dec != dec.to_integral_value():
        raise CellError(f"{prefix}_invalid", f"{label} должен быть текстом или целым числом.")
    return str(int(dec))


def map_header(
    header: tuple[Cell, ...],
    fields: Mapping[str, str],
    required: frozenset[str],
    sheet: str,
    issues: list[ImportIssue],
) -> dict[str, int] | None:
    """Map contract fields to column indexes, or None if the header is unusable.

    Unknown columns are ignored with a warning; a formula, a duplicate or a missing
    required column makes the sheet unusable.
    """
    mapped: dict[str, int] = {}
    fatal = False
    for index, cell in enumerate(header):
        column = column_letter(index + 1)
        if isinstance(cell, FormulaCell):
            issues.append(
                issue(
                    sheet,
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
        if title not in fields:
            issues.append(
                issue(
                    sheet,
                    "unknown_column",
                    IssueSeverity.WARNING,
                    f"Неизвестная колонка «{title}» игнорируется.",
                    1,
                    column,
                )
            )
            continue
        field = fields[title]
        if field in mapped:
            issues.append(
                issue(
                    sheet,
                    "duplicate_column",
                    IssueSeverity.ERROR,
                    f"Колонка «{title}» указана несколько раз.",
                    1,
                    column,
                )
            )
            fatal = True
            continue
        mapped[field] = index
    titles = {field: title for title, field in fields.items()}
    for field in sorted(required - mapped.keys()):
        issues.append(
            issue(
                sheet,
                f"{field}_column_missing",
                IssueSeverity.ERROR,
                f"Обязательная колонка «{titles[field]}» отсутствует.",
            )
        )
        fatal = True
    return None if fatal else mapped


def data_rows(
    rows: Iterable[tuple[int, tuple[Cell, ...]]],
    max_rows: int,
    sheet: str,
    issues: list[ImportIssue],
) -> Iterator[tuple[int, tuple[Cell, ...]]]:
    """Non-blank data rows up to the limit; exceeding it is one error and the rest is cut."""
    seen = 0
    for row_number, cells in rows:
        seen += 1
        if seen > max_rows:
            issues.append(
                issue(
                    sheet,
                    "row_limit",
                    IssueSeverity.ERROR,
                    f"Превышен лимит строк ({max_rows}).",
                    row_number,
                )
            )
            return
        if all(is_blank(cell) for cell in cells):
            continue
        yield row_number, cells


def cell_at(cells: tuple[Cell, ...], mapping: Mapping[str, int], field: str) -> Cell:
    index = mapping.get(field)
    return cells[index] if index is not None and index < len(cells) else None
