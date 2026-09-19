"""JSON payloads of saved pipeline steps (S3-01).

The step store keeps an opaque string per step; this module is the only place that knows
its shape. Domain results (snapshots, assessments) are serialized by B's
``domain/serialization.py``; the import step (rows and issues of the «Контрагенты» file)
is A's storage concern and lives here. Loading goes through the domain constructors, so a
payload that no longer matches the domain rules fails with ``PayloadError`` instead of
yielding a half-valid object, and errors never echo the payload.
"""

import json
import re
from datetime import date
from decimal import Decimal

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import ExternalSnapshot
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.serialization import (
    SCHEMA,
    PayloadError,
    snapshot_from_dict,
    snapshot_to_dict,
)

__all__ = [
    "PayloadError",
    "dump_import",
    "dump_snapshots",
    "load_import",
    "load_snapshots",
]


_INN = re.compile(r"[0-9]{10}")


def _typed(value: object, kind: type) -> object:
    # Exact types: JSON `true` must not pass as the int 1, a number not as a string.
    if type(value) is not kind:
        raise PayloadError(f"expected {kind.__name__}")
    return value


def _optional(value: object, kind: type) -> object:
    return None if value is None else _typed(value, kind)


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _load_day(value: object) -> date | None:
    return None if value is None else date.fromisoformat(_typed(value, str))


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _load_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(_typed(value, str))


# --- import step -------------------------------------------------------------------


def _row(row: CounterpartyRow) -> dict:
    return {
        "inn": row.inn,
        "name": row.name,
        "cutoff_date": _day(row.cutoff_date),
        "debt": _decimal(row.debt),
        "overdue_days": row.overdue_days,
        "last_payment_date": _day(row.last_payment_date),
    }


def _load_row(data: dict) -> CounterpartyRow:
    inn = _typed(data["inn"], str)
    if not _INN.fullmatch(inn):
        raise PayloadError("row INN must be 10 digits")
    return CounterpartyRow(
        inn=inn,
        name=_optional(data.get("name"), str),
        cutoff_date=_load_day(data.get("cutoff_date")),
        debt=_load_decimal(data.get("debt")),
        overdue_days=_optional(data.get("overdue_days"), int),
        last_payment_date=_load_day(data.get("last_payment_date")),
    )


def _issue(issue: ImportIssue) -> dict:
    return {
        "code": issue.code,
        "severity": issue.severity.value,
        "sheet": issue.sheet,
        "reason": issue.reason,
        "file_id": issue.file_id,
        "row": issue.row,
        "column": issue.column,
    }


def _load_issue(data: dict) -> ImportIssue:
    return ImportIssue(
        code=_typed(data["code"], str),
        severity=IssueSeverity(_typed(data["severity"], str)),
        sheet=_typed(data["sheet"], str),
        reason=_typed(data["reason"], str),
        file_id=_optional(data.get("file_id"), str),
        row=_optional(data.get("row"), int),
        column=_optional(data.get("column"), str),
    )


def dump_import(rows: tuple[CounterpartyRow, ...], issues: tuple[ImportIssue, ...]) -> str:
    return json.dumps(
        {
            "schema": SCHEMA,
            "rows": [_row(row) for row in rows],
            "issues": [_issue(issue) for issue in issues],
        },
        ensure_ascii=False,
    )


def load_import(payload: str) -> tuple[tuple[CounterpartyRow, ...], tuple[ImportIssue, ...]]:
    try:
        data = json.loads(payload)
        if data.get("schema") != SCHEMA:
            raise PayloadError("import payload has another schema")
        rows = tuple(_load_row(item) for item in data["rows"])
        issues = tuple(_load_issue(item) for item in data["issues"])
    except PayloadError:
        raise
    except (ValueError, ArithmeticError, KeyError, TypeError, AttributeError) as exc:
        # ArithmeticError: Decimal("abc") raises InvalidOperation, not ValueError.
        raise PayloadError("import payload is not readable") from exc
    return rows, issues


# --- external fetch step -----------------------------------------------------------


def dump_snapshots(snapshots: tuple[ExternalSnapshot, ...]) -> str:
    """All sections of one INN in request order, each in B's snapshot format."""
    return json.dumps([snapshot_to_dict(snapshot) for snapshot in snapshots], ensure_ascii=False)


def load_snapshots(payload: str) -> tuple[ExternalSnapshot, ...]:
    try:
        data = json.loads(payload)
        if not isinstance(data, list):
            raise PayloadError("snapshot payload must be a list of sections")
        return tuple(snapshot_from_dict(item) for item in data)
    except PayloadError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise PayloadError("snapshot payload is not readable") from exc
