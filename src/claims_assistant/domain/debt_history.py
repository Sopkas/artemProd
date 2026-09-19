"""S4-05: the optional «История долга» sheet (docs/data-contracts.md, section 4).

One row per INN and cut-off date with the debt on that date. Identical duplicates merge,
conflicting ones are errors. Monthly dynamics and the comparison with the current cut-off
of the «Контрагенты» file are indicators (S4-06), not part of the import.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claims_assistant.domain.counterparties import ImportLimits
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.sheet_rules import (
    Cell,
    CellError,
    cell_at,
    column_letter,
    data_rows,
    issue,
    map_header,
    parse_amount,
    parse_date,
    parse_inn,
)

SHEET_NAME = "История долга"
COLUMN_TITLES = ("ИНН", "Дата среза", "Сумма долга")
_FIELDS = {"ИНН": "inn", "Дата среза": "cutoff_date", "Сумма долга": "debt"}
_REQUIRED = frozenset(_FIELDS.values())


@dataclass(frozen=True, slots=True)
class DebtSnapshot:
    inn: str
    cutoff_date: date
    debt: Decimal

    def __post_init__(self) -> None:
        if not self.debt.is_finite() or self.debt < 0:
            raise ValueError("Debt must be a finite, non-negative Decimal")


@dataclass(frozen=True, slots=True)
class DebtHistoryImport:
    rows: tuple[DebtSnapshot, ...] = ()
    issues: tuple[ImportIssue, ...] = ()


def _required(value: object, code: str, reason: str) -> object:
    if value is None:
        raise CellError(code, reason)
    return value


def parse_debt_history(
    header: tuple[Cell, ...],
    rows: Iterable[tuple[int, tuple[Cell, ...]]],
    *,
    known_inns: Collection[str] | None,
    analysis_date: date | None,
    limits: ImportLimits = ImportLimits(),
) -> DebtHistoryImport:
    """Validate the sheet: snapshots of package counterparties up to the analysis date."""
    issues: list[ImportIssue] = []
    mapping = map_header(header, _FIELDS, _REQUIRED, SHEET_NAME, issues)
    if mapping is None:
        return DebtHistoryImport(issues=tuple(issues))

    def column(field: str) -> str:
        return column_letter(mapping[field] + 1)

    rules = {
        "inn": parse_inn,
        "cutoff_date": lambda c: _required(
            parse_date(c), "cutoff_missing", "Дата среза обязательна."
        ),
        "debt": lambda c: _required(
            parse_amount(c, prefix="debt", label="Сумма долга"),
            "debt_missing",
            "Сумма долга обязательна.",
        ),
    }
    accepted: dict[tuple[str, date], tuple[int, DebtSnapshot]] = {}
    for row_number, cells in data_rows(rows, limits.max_rows, SHEET_NAME, issues):
        values: dict[str, object] = {}
        row_issues = []
        for field, rule in rules.items():
            try:
                values[field] = rule(cell_at(cells, mapping, field))
            except CellError as error:
                row_issues.append(
                    issue(
                        SHEET_NAME,
                        error.code,
                        IssueSeverity.ERROR,
                        error.reason,
                        row_number,
                        column(field),
                    )
                )
        if row_issues:
            issues.extend(row_issues)
            continue
        snapshot = DebtSnapshot(**values)
        if known_inns is not None and snapshot.inn not in known_inns:
            issues.append(
                issue(
                    SHEET_NAME,
                    "inn_not_in_package",
                    IssueSeverity.ERROR,
                    "ИНН нет в файле «Контрагенты»; срез не добавляет контрагента.",
                    row_number,
                    column("inn"),
                )
            )
            continue
        if analysis_date is not None and snapshot.cutoff_date > analysis_date:
            issues.append(
                issue(
                    SHEET_NAME,
                    "cutoff_after_analysis_date",
                    IssueSeverity.WARNING,
                    "Срез позже даты анализа и исключён.",
                    row_number,
                    column("cutoff_date"),
                )
            )
            continue
        key = (snapshot.inn, snapshot.cutoff_date)
        existing = accepted.get(key)
        if existing is None:
            accepted[key] = (row_number, snapshot)
        elif existing[1] == snapshot:
            issues.append(
                issue(
                    SHEET_NAME,
                    "duplicate_row",
                    IssueSeverity.WARNING,
                    f"Строка полностью дублирует строку {existing[0]} и объединена.",
                    row_number,
                )
            )
        else:
            issues.append(
                issue(
                    SHEET_NAME,
                    "snapshot_conflict",
                    IssueSeverity.ERROR,
                    f"Другая сумма на ту же дату среза уже в строке {existing[0]}; "
                    "требуется исправление.",
                    row_number,
                    column("debt"),
                )
            )
    return DebtHistoryImport(
        rows=tuple(snapshot for _, snapshot in accepted.values()), issues=tuple(issues)
    )
