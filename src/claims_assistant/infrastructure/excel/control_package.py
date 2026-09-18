"""S2-06: a synthetic control package of 50 counterparties with known outcomes.

One deterministic «Контрагенты» workbook that exercises the S2-04 parser end to end:
38 usable rows plus 12 crafted rows that must each raise a specific ``ImportIssue``.
The expected issues are declared next to the rows that produce them, so the builder and
the acceptance list in ``docs/control-package.md`` never drift apart.

Every usable counterparty also carries a *predefined demo priority* — a fixed showcase
value for the demo Excel report (S2-05), explicitly marked demo. Real priorities are
computed only in sprint 3 (see ``docs/scoring.md``); nothing here is calculated.
"""

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from openpyxl import Workbook

from claims_assistant.domain.counterparties import COLUMN_TITLES, SHEET_NAME, Cell
from claims_assistant.domain.imports import IssueSeverity

# The cut-off every dated row must match; the package is built for this analysis date.
ANALYSIS_DATE = date(2026, 9, 1)

_LEGAL_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)


class DemoPriority(StrEnum):
    """Predefined demo score for the report; see docs/scoring.md for the real rules."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExpectedIssue:
    """One issue the parser must report for a row, keyed by Excel row (header is row 1)."""

    row: int
    code: str
    severity: IssueSeverity
    column: str | None = None


@dataclass(frozen=True, slots=True)
class DemoScore:
    """A usable counterparty and the demo priority the report should show for it."""

    inn: str
    priority: DemoPriority


@dataclass(frozen=True, slots=True)
class _RowSpec:
    cells: list[Cell]
    demo: DemoPriority | None = None  # set for usable rows shown in the demo report
    issues: tuple[ExpectedIssue, ...] = ()
    usable: bool = False  # True only when the row becomes a distinct counterparty


def _inn10(prefix9: str) -> str:
    """A 10-digit legal INN with a valid control digit, so valid rows round-trip."""
    control = sum(w * int(d) for w, d in zip(_LEGAL_WEIGHTS, prefix9, strict=True)) % 11 % 10
    return prefix9 + str(control)


def _valid_inn(index: int) -> str:
    return _inn10(f"{770000000 + index}")


def _edge_inn(index: int) -> str:
    return _inn10(f"{780000000 + index}")


def _cells(
    inn: Cell,
    name: Cell = None,
    cutoff: date | None = None,
    debt: Decimal | None = None,
    overdue: int | None = None,
    last_payment: date | None = None,
) -> list[Cell]:
    # Column order matches COLUMN_TITLES; debt is written as a float like the S2-04 template.
    return [inn, name, cutoff, None if debt is None else float(debt), overdue, last_payment]


# Fixed profiles per demo priority. UNKNOWN rows carry only an INN (no internal signals);
# CRITICAL mirrors a HIGH row but is a hand-assigned demo score (a real critical needs a
# confirmed external event, which no counterparties file can provide).
_LAST_PAYMENT = date(2026, 7, 15)


def _profile_cells(index: int, priority: DemoPriority) -> list[Cell]:
    inn = _valid_inn(index)
    name = f"ООО «Контрагент {index:02d}»"
    debt = Decimal("50000.00") + Decimal(index) * Decimal("1000.00")
    if priority is DemoPriority.UNKNOWN:
        return _cells(inn, name)
    overdue = {
        DemoPriority.LOW: 0,
        DemoPriority.MEDIUM: 45,
        DemoPriority.HIGH: 90,
        DemoPriority.CRITICAL: 90,
    }[priority]
    return _cells(inn, name, ANALYSIS_DATE, debt, overdue, _LAST_PAYMENT)


def _valid_specs() -> list[_RowSpec]:
    # Row 0 is INN-only (referenced by the inn_conflict edge row); row 1 is a full LOW row
    # (copied verbatim by the duplicate_row edge row). The rest cycle through the buckets.
    specs = [
        _RowSpec(_profile_cells(0, DemoPriority.UNKNOWN), DemoPriority.UNKNOWN, usable=True),
        _RowSpec(_profile_cells(1, DemoPriority.LOW), DemoPriority.LOW, usable=True),
    ]
    rotation = (
        DemoPriority.MEDIUM,
        DemoPriority.HIGH,
        DemoPriority.CRITICAL,
        DemoPriority.LOW,
        DemoPriority.UNKNOWN,
    )
    for index in range(2, 38):
        priority = rotation[(index - 2) % len(rotation)]
        specs.append(_RowSpec(_profile_cells(index, priority), priority, usable=True))
    return specs


def _edge_specs(first_row: int, low_row_cells: list[Cell]) -> list[_RowSpec]:
    # first_row is the Excel row of the first edge row; issues are keyed by absolute row.
    r = first_row
    conflict_inn = _valid_inn(0)  # same INN as the INN-only row 0 → data conflict
    specs = [
        _RowSpec(
            _cells(None, "Контрагент без ИНН"),
            issues=(ExpectedIssue(r, "inn_missing", IssueSeverity.ERROR, "A"),),
        ),
        _RowSpec(
            _cells("0000000001", "Неверная контрольная цифра"),
            issues=(ExpectedIssue(r + 1, "inn_invalid", IssueSeverity.ERROR, "A"),),
        ),
        _RowSpec(
            _cells(_edge_inn(0), "=1+1"),
            issues=(ExpectedIssue(r + 2, "formula_forbidden", IssueSeverity.ERROR, "B"),),
        ),
        _RowSpec(
            [_edge_inn(1), "Долг не число", ANALYSIS_DATE, "abc", None, None],
            issues=(ExpectedIssue(r + 3, "debt_not_number", IssueSeverity.ERROR, "D"),),
        ),
        _RowSpec(
            [_edge_inn(2), "Отрицательный долг", ANALYSIS_DATE, -1000.0, None, None],
            issues=(ExpectedIssue(r + 4, "debt_negative", IssueSeverity.ERROR, "D"),),
        ),
        _RowSpec(
            [_edge_inn(3), "Просрочка не целое", ANALYSIS_DATE, None, "пять", None],
            issues=(ExpectedIssue(r + 5, "overdue_not_integer", IssueSeverity.ERROR, "E"),),
        ),
        _RowSpec(
            _cells(_edge_inn(4), "Нет даты среза", debt=Decimal("5000.00")),
            issues=(ExpectedIssue(r + 6, "cutoff_required", IssueSeverity.ERROR, None),),
        ),
        _RowSpec(
            _cells(_edge_inn(5), "Дата среза не та", date(2026, 8, 1), Decimal("5000.00")),
            issues=(ExpectedIssue(r + 7, "cutoff_mismatch", IssueSeverity.ERROR, "C"),),
        ),
        _RowSpec(
            _cells(
                _edge_inn(6),
                "Платёж в будущем",
                ANALYSIS_DATE,
                Decimal("5000.00"),
                10,
                date(2026, 12, 31),
            ),
            issues=(ExpectedIssue(r + 8, "last_payment_future", IssueSeverity.ERROR, "F"),),
        ),
        _RowSpec(
            _cells(conflict_inn, "Конфликт по ИНН", ANALYSIS_DATE, Decimal("12345.00")),
            issues=(ExpectedIssue(r + 9, "inn_conflict", IssueSeverity.ERROR, "A"),),
        ),
        _RowSpec(
            list(low_row_cells),  # verbatim copy of the row-1 LOW counterparty
            issues=(ExpectedIssue(r + 10, "duplicate_row", IssueSeverity.WARNING, None),),
        ),
        _RowSpec(
            [_edge_inn(7), 999, None, None, None, None],
            issues=(ExpectedIssue(r + 11, "name_invalid", IssueSeverity.ERROR, "B"),),
        ),
    ]
    return specs


def _all_specs() -> list[_RowSpec]:
    valid = _valid_specs()
    # Edge rows follow the valid rows; +2 skips the header (row 1) and 0-based offset.
    edges = _edge_specs(len(valid) + 2, valid[1].cells)
    return valid + edges


_SPECS = _all_specs()

# Public acceptance data derived from the single source of truth above.
EXPECTED_USABLE: int = sum(1 for spec in _SPECS if spec.usable)
EXPECTED_ISSUES: tuple[ExpectedIssue, ...] = tuple(
    issue for spec in _SPECS for issue in spec.issues
)
DEMO_SCORES: tuple[DemoScore, ...] = tuple(
    DemoScore(spec.cells[0], spec.demo)
    for spec in _SPECS
    if spec.usable and spec.demo is not None and isinstance(spec.cells[0], str)
)


def build_control_package() -> bytes:
    """Return the bytes of the 50-row synthetic «Контрагенты» control workbook."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = SHEET_NAME
    worksheet.append(list(COLUMN_TITLES))
    # Keep the INN column textual so leading digits and control digits are never lost.
    for cell in worksheet["A"]:
        cell.number_format = "@"
    for spec in _SPECS:
        worksheet.append(spec.cells)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
