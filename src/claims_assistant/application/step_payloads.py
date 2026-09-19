"""JSON payloads of saved pipeline steps (S3-01).

The step store keeps an opaque string per step; this module is the only place that knows
its shape. Every domain value — snapshots, «Контрагенты» rows, import issues — is
serialized by B's ``domain/serialization.py``; this module only wraps them into step
payloads (the import step's envelope and schema, the list of sections of one INN).
Loading goes through the domain constructors, so a payload that no longer matches the
domain rules fails with ``PayloadError`` instead of yielding a half-valid object, and
errors never echo the payload.
"""

import json

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import ExternalSnapshot
from claims_assistant.domain.imports import ImportIssue
from claims_assistant.domain.serialization import (
    SCHEMA,
    PayloadError,
    counterparty_row_from_dict,
    counterparty_row_to_dict,
    import_issue_from_dict,
    import_issue_to_dict,
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


def dump_import(rows: tuple[CounterpartyRow, ...], issues: tuple[ImportIssue, ...]) -> str:
    return json.dumps(
        {
            "schema": SCHEMA,
            "rows": [counterparty_row_to_dict(row) for row in rows],
            "issues": [import_issue_to_dict(issue) for issue in issues],
        },
        ensure_ascii=False,
    )


def load_import(payload: str) -> tuple[tuple[CounterpartyRow, ...], tuple[ImportIssue, ...]]:
    try:
        data = json.loads(payload)
        if data.get("schema") != SCHEMA:
            raise PayloadError("import payload has another schema")
        rows = tuple(counterparty_row_from_dict(item) for item in data["rows"])
        issues = tuple(import_issue_from_dict(item) for item in data["issues"])
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
