"""S4-02: the optional «Взаимодействия» sheet (docs/data-contracts.md, section 3).

One row per contact with a counterparty: an ID unique within the INN, a date, the
employee's comment and an optional channel. Rows are validated and kept only for
counterparties of the package; repeated IDs follow the payments rules. The result is a
chronology — rows in date order per INN — shown without any AI reading (sprint 5 may
extract a payment promise from a comment, but never changes the numeric scoring).

Comments are the customer's business text: they stay in the row and never appear in an
issue reason or a log line.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date

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
    parse_date,
    parse_inn,
    parse_record_id,
    reject_formula,
)

SHEET_NAME = "Взаимодействия"
COLUMN_TITLES = ("ИНН", "ID взаимодействия", "Дата взаимодействия", "Комментарий", "Канал")
_FIELDS = {
    "ИНН": "inn",
    "ID взаимодействия": "interaction_id",
    "Дата взаимодействия": "happened_on",
    "Комментарий": "comment",
    "Канал": "channel",
}
_REQUIRED = frozenset({"inn", "interaction_id", "happened_on", "comment"})
MAX_COMMENT_CHARS = 2000
MAX_CHANNEL_CHARS = 50


@dataclass(frozen=True, slots=True)
class InteractionRow:
    inn: str
    interaction_id: str
    happened_on: date
    comment: str
    channel: str | None = None

    def __post_init__(self) -> None:
        if not self.comment.strip():
            raise ValueError("Comment must not be empty")


@dataclass(frozen=True, slots=True)
class InteractionsImport:
    rows: tuple[InteractionRow, ...] = ()  # chronology: by INN, then by date, then by row
    issues: tuple[ImportIssue, ...] = ()


def _text(cell: Cell, *, code: str, label: str, max_chars: int, required: bool) -> str | None:
    reject_formula(cell)
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        if required:
            raise CellError(f"{code}_missing", f"{label} обязателен.")
        return None
    if isinstance(cell, bool) or not isinstance(cell, (str, int, float)):
        raise CellError(f"{code}_invalid", f"{label} должен быть текстом.")
    text = str(cell).strip()
    if len(text) > max_chars:
        raise CellError(f"{code}_too_long", f"{label} длиннее {max_chars} символов.")
    return text


def _required_date(cell: Cell) -> date:
    value = parse_date(cell)
    if value is None:
        raise CellError("interaction_date_missing", "Дата взаимодействия обязательна.")
    return value


def chronology(rows: Iterable[InteractionRow]) -> dict[str, tuple[InteractionRow, ...]]:
    """Rows per INN in date order; equal dates keep the sheet order."""
    by_inn: dict[str, list[InteractionRow]] = {}
    for row in rows:
        by_inn.setdefault(row.inn, []).append(row)
    return {
        inn: tuple(sorted(items, key=lambda item: item.happened_on))
        for inn, items in by_inn.items()
    }


def parse_interactions(
    header: tuple[Cell, ...],
    rows: Iterable[tuple[int, tuple[Cell, ...]]],
    *,
    known_inns: Collection[str] | None,
    analysis_date: date | None,
    limits: ImportLimits = ImportLimits(),
) -> InteractionsImport:
    """Validate the sheet: interactions of package counterparties up to the analysis date.

    ``known_inns`` are the INNs of the «Контрагенты» file; a row of any other INN never
    adds a counterparty and is reported. ``None`` skips that check (no package context).
    """
    issues: list[ImportIssue] = []
    mapping = map_header(header, _FIELDS, _REQUIRED, SHEET_NAME, issues)
    if mapping is None:
        return InteractionsImport(issues=tuple(issues))

    def column(field: str) -> str:
        return column_letter(mapping[field] + 1)

    rules = {
        "inn": parse_inn,
        "interaction_id": lambda c: parse_record_id(
            c, prefix="interaction_id", label="ID взаимодействия"
        ),
        "happened_on": _required_date,
        "comment": lambda c: _text(
            c, code="comment", label="Комментарий", max_chars=MAX_COMMENT_CHARS, required=True
        ),
        "channel": lambda c: _text(
            c, code="channel", label="Канал", max_chars=MAX_CHANNEL_CHARS, required=False
        ),
    }
    accepted: dict[tuple[str, str], tuple[int, InteractionRow]] = {}
    for row_number, cells in data_rows(rows, limits.max_rows, SHEET_NAME, issues):
        values: dict[str, object] = {}
        row_issues = []
        for field, rule in rules.items():
            if field == "channel" and "channel" not in mapping:
                values[field] = None
                continue
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
        interaction = InteractionRow(**values)
        if known_inns is not None and interaction.inn not in known_inns:
            issues.append(
                issue(
                    SHEET_NAME,
                    "inn_not_in_package",
                    IssueSeverity.ERROR,
                    "ИНН нет в файле «Контрагенты»; запись не добавляет контрагента.",
                    row_number,
                    column("inn"),
                )
            )
            continue
        if analysis_date is not None and interaction.happened_on > analysis_date:
            issues.append(
                issue(
                    SHEET_NAME,
                    "interaction_after_analysis_date",
                    IssueSeverity.WARNING,
                    "Взаимодействие позже даты анализа и исключено.",
                    row_number,
                    column("happened_on"),
                )
            )
            continue
        key = (interaction.inn, interaction.interaction_id)
        existing = accepted.get(key)
        if existing is None:
            accepted[key] = (row_number, interaction)
        elif existing[1] == interaction:
            issues.append(
                issue(
                    SHEET_NAME,
                    "duplicate_interaction",
                    IssueSeverity.WARNING,
                    f"Строка повторяет запись из строки {existing[0]} и пропущена.",
                    row_number,
                )
            )
        else:
            issues.append(
                issue(
                    SHEET_NAME,
                    "interaction_id_conflict",
                    IssueSeverity.ERROR,
                    f"ID взаимодействия уже встречался в строке {existing[0]} с другими данными.",
                    row_number,
                    column("interaction_id"),
                )
            )
    ordered = tuple(
        row
        for inn_rows in chronology(row for _, row in accepted.values()).values()
        for row in inn_rows
    )
    return InteractionsImport(rows=ordered, issues=tuple(issues))
