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
from datetime import date
from decimal import Decimal
from typing import Any

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.debt_report import ContractDebt, CounterpartyDebt
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
    "dump_links",
    "dump_snapshots",
    "load_import",
    "load_links",
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
    # S7-01: the overdue report's counterparties with their contracts, before they are
    # linked to an INN — the link is made by name when the report is built.
    contracts: tuple[CounterpartyDebt, ...] = ()


def dump_import(package: ImportedPackage) -> str:
    return json.dumps(
        {
            "schema": SCHEMA,
            "rows": [counterparty_row_to_dict(row) for row in package.rows],
            "issues": [import_issue_to_dict(issue) for issue in package.issues],
            "payments": [payment_row_to_dict(row) for row in package.payments],
            "history": [debt_snapshot_to_dict(row) for row in package.history],
            "interactions": [interaction_row_to_dict(row) for row in package.interactions],
            "contracts": [_counterparty_debt_to_dict(row) for row in package.contracts],
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
            contracts=tuple(
                _counterparty_debt_from_dict(item) for item in data.get("contracts", ())
            ),
        )
    except PayloadError:
        raise
    except (ValueError, ArithmeticError, KeyError, TypeError, AttributeError) as exc:
        # ArithmeticError: Decimal("abc") raises InvalidOperation, not ValueError.
        raise PayloadError("import payload is not readable") from exc
    return package


def dump_links(by_inn: dict[str, tuple[ContractDebt, ...]]) -> str:
    """Whose contracts are whose, as the report decided it (S7-01).

    Stored so the card reads the same answer instead of working it out again from less:
    it knows one company's name, and could not tell two namesakes apart (review B on #58).
    """
    return json.dumps(
        {
            "schema": SCHEMA,
            "contracts": {
                inn: [_contract_to_dict(contract) for contract in contracts]
                for inn, contracts in by_inn.items()
            },
        },
        ensure_ascii=False,
    )


def load_links(payload: str) -> dict[str, tuple[ContractDebt, ...]]:
    try:
        data = json.loads(payload)
        if data.get("schema") != SCHEMA:
            raise PayloadError("contract link payload has another schema")
        return {
            inn: tuple(_contract_from_dict(item) for item in contracts)
            for inn, contracts in data["contracts"].items()
        }
    except PayloadError:
        raise
    except (ValueError, ArithmeticError, KeyError, TypeError, AttributeError) as exc:
        raise PayloadError("contract link payload is not readable") from exc


# --- the overdue report (S7-01) ----------------------------------------------------
# Kept here rather than in ``domain/serialization`` because the report's rows are new and
# B moved the package codecs there himself (#39); moving these two is his call.


def _counterparty_debt_to_dict(row: CounterpartyDebt) -> dict[str, Any]:
    return {
        "name": row.name,
        "inn": row.inn,
        "group": row.group,
        "note": row.note,
        "contracts": [_contract_to_dict(contract) for contract in row.contracts],
    }


def _contract_to_dict(contract: ContractDebt) -> dict[str, Any]:
    return {
        "name": contract.name,
        "overdue": None if contract.overdue is None else format(contract.overdue, "f"),
        "days": contract.days,
        "due_until": None if contract.due_until is None else contract.due_until.isoformat(),
        "subject": contract.subject,
        "note": contract.note,
    }


def _contract_from_dict(item: Any) -> ContractDebt:
    if not isinstance(item, dict):
        raise PayloadError("contract must be an object")
    return ContractDebt(
        name=_text(item, "name"),
        overdue=None if item.get("overdue") is None else Decimal(str(item["overdue"])),
        days=None if item.get("days") is None else _whole(item["days"]),
        due_until=None
        if item.get("due_until") is None
        else date.fromisoformat(str(item["due_until"])),
        subject=_optional_text(item, "subject"),
        note=_optional_text(item, "note"),
    )


def _counterparty_debt_from_dict(data: Any) -> CounterpartyDebt:
    if not isinstance(data, dict):
        raise PayloadError("contract payload must be an object")
    contracts = data.get("contracts") or ()
    if not isinstance(contracts, list):
        raise PayloadError("contracts must be a list")
    return CounterpartyDebt(
        name=_text(data, "name"),
        inn=_optional_text(data, "inn"),
        group=_optional_text(data, "group"),
        note=_optional_text(data, "note"),
        contracts=tuple(_contract_from_dict(item) for item in contracts),
    )


def _text(data: Any, key: str) -> str:
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise PayloadError(f"{key} must be a non-empty string")
    return value


def _optional_text(data: Any, key: str) -> str | None:
    value = data.get(key) if isinstance(data, dict) else None
    if value is None:
        return None
    if not isinstance(value, str):
        raise PayloadError(f"{key} must be a string")
    return value


def _whole(value: Any) -> int:
    if type(value) is not int:
        raise PayloadError("days must be an integer")
    return value


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
