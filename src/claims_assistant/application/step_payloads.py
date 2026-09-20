"""JSON payloads of saved pipeline steps (S3-01).

The step store keeps an opaque string per step; this module is the only place that knows
its shape. Every domain value — snapshots, «Контрагенты» rows, import issues, and the
rows of the optional package files — is serialized by ``domain/serialization.py``; this
module only wraps them into step payloads (the import step's envelope and schema, the
list of sections of one INN).
Loading goes through the domain constructors, so a payload that no longer matches the
domain rules fails with ``PayloadError`` instead of yielding a half-valid object, and
errors never echo the payload.
"""

import json
from dataclasses import dataclass

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.external import ExternalSnapshot
from claims_assistant.domain.imports import ImportIssue
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.serialization import (
    SCHEMA,
    PayloadError,
    counterparty_row_from_dict,
    counterparty_row_to_dict,
    debt_snapshot_from_dict,
    debt_snapshot_to_dict,
    import_issue_from_dict,
    import_issue_to_dict,
    interaction_row_from_dict,
    interaction_row_to_dict,
    payment_row_from_dict,
    payment_row_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
)

__all__ = [
    "ImportedPackage",
    "PayloadError",
    "dump_import",
    "dump_snapshots",
    "load_import",
    "load_snapshots",
]


@dataclass(frozen=True, slots=True)
class ImportedPackage:
    """What the import step keeps of the whole package (S4-04): the rows and issues of
    every file, so a resumed run never re-reads the optional files either."""

    rows: tuple[CounterpartyRow, ...]
    issues: tuple[ImportIssue, ...]
    payments: tuple[PaymentRow, ...] = ()
    history: tuple[DebtSnapshot, ...] = ()
    interactions: tuple[InteractionRow, ...] = ()


def dump_import(package: ImportedPackage) -> str:
    return json.dumps(
        {
            "schema": SCHEMA,
            "rows": [counterparty_row_to_dict(row) for row in package.rows],
            "issues": [import_issue_to_dict(issue) for issue in package.issues],
            "payments": [payment_row_to_dict(row) for row in package.payments],
            "history": [debt_snapshot_to_dict(row) for row in package.history],
            "interactions": [interaction_row_to_dict(row) for row in package.interactions],
        },
        ensure_ascii=False,
    )


def load_import(payload: str) -> ImportedPackage:
    try:
        data = json.loads(payload)
        if data.get("schema") != SCHEMA:
            raise PayloadError("import payload has another schema")
        package = ImportedPackage(
            rows=tuple(counterparty_row_from_dict(item) for item in data["rows"]),
            issues=tuple(import_issue_from_dict(item) for item in data["issues"]),
            payments=tuple(payment_row_from_dict(item) for item in data["payments"]),
            history=tuple(debt_snapshot_from_dict(item) for item in data["history"]),
            interactions=tuple(interaction_row_from_dict(item) for item in data["interactions"]),
        )
    except PayloadError:
        raise
    except (ValueError, ArithmeticError, KeyError, TypeError, AttributeError) as exc:
        # ArithmeticError: Decimal("abc") raises InvalidOperation, not ValueError.
        raise PayloadError("import payload is not readable") from exc
    return package


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
