"""JSON payloads of saved pipeline steps (S3-01).

The step store keeps an opaque string per step; this module is the only place that knows
its shape. Loading goes through the domain constructors, so a payload that no longer
matches the domain rules fails loudly instead of yielding a half-valid object. Values keep
their exact types: a Decimal stays a Decimal, a date stays a date, a CompanyStatus stays
an enum — the report must not depend on how a value was stored.
"""

import json
from datetime import date, datetime
from decimal import Decimal

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    DataMode,
    Evidence,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    Period,
    ProviderError,
    Section,
)
from claims_assistant.domain.imports import ImportIssue, IssueSeverity


class PayloadError(ValueError):
    """The saved payload cannot be turned back into domain objects."""


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _load_day(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _load_moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _load_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


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
    return CounterpartyRow(
        inn=data["inn"],
        name=data.get("name"),
        cutoff_date=_load_day(data.get("cutoff_date")),
        debt=_load_decimal(data.get("debt")),
        overdue_days=data.get("overdue_days"),
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
        code=data["code"],
        severity=IssueSeverity(data["severity"]),
        sheet=data["sheet"],
        reason=data["reason"],
        file_id=data.get("file_id"),
        row=data.get("row"),
        column=data.get("column"),
    )


def dump_import(rows: tuple[CounterpartyRow, ...], issues: tuple[ImportIssue, ...]) -> str:
    return json.dumps(
        {"rows": [_row(row) for row in rows], "issues": [_issue(issue) for issue in issues]},
        ensure_ascii=False,
    )


def load_import(payload: str) -> tuple[tuple[CounterpartyRow, ...], tuple[ImportIssue, ...]]:
    try:
        data = json.loads(payload)
        rows = tuple(_load_row(item) for item in data["rows"])
        issues = tuple(_load_issue(item) for item in data["issues"])
    except (ValueError, KeyError, TypeError) as exc:
        raise PayloadError("import payload is not readable") from exc
    return rows, issues


# --- external fetch step -----------------------------------------------------------


def _period(period: Period | None) -> dict | None:
    if period is None:
        return None
    return {"start": _day(period.start), "end": _day(period.end)}


def _load_period(data: dict | None) -> Period | None:
    if data is None:
        return None
    return Period(start=_load_day(data["start"]), end=_load_day(data["end"]))


def _value(value: object) -> dict:
    # Exact types: bool before int, CompanyStatus before str (both are subclasses).
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, CompanyStatus):
        return {"type": "company_status", "value": value.value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    raise PayloadError(f"unsupported fact value type {type(value).__name__}")


def _load_value(data: dict) -> object:
    kind = data["type"]
    if kind == "null":
        return None
    raw = data["value"]
    if kind == "bool":
        return bool(raw)
    if kind == "company_status":
        return CompanyStatus(raw)
    if kind == "int":
        return int(raw)
    if kind == "decimal":
        return Decimal(raw)
    if kind == "date":
        return date.fromisoformat(raw)
    if kind == "str":
        return str(raw)
    raise PayloadError(f"unknown fact value type {kind}")


def _fact(fact: Fact) -> dict:
    return {
        "id": fact.id,
        "inn": fact.inn,
        "kind": fact.kind.value,
        "value": _value(fact.value),
        "evidence_ids": list(fact.evidence_ids),
        "observed_on": _day(fact.observed_on),
        "period": _period(fact.period),
        "unit": fact.unit,
        "missing_reason": fact.missing_reason,
    }


def _load_fact(data: dict) -> Fact:
    return Fact(
        id=data["id"],
        inn=data["inn"],
        kind=FactKind(data["kind"]),
        value=_load_value(data["value"]),
        evidence_ids=tuple(data["evidence_ids"]),
        observed_on=_load_day(data.get("observed_on")),
        period=_load_period(data.get("period")),
        unit=data.get("unit"),
        missing_reason=data.get("missing_reason"),
    )


def _evidence(item: Evidence) -> dict:
    return {"id": item.id, "source": item.source, "record_id": item.record_id, "url": item.url}


def _load_evidence(data: dict) -> Evidence:
    return Evidence(
        id=data["id"], source=data["source"], record_id=data["record_id"], url=data.get("url")
    )


def _error(error: ProviderError | None) -> dict | None:
    if error is None:
        return None
    return {
        "code": error.code,
        "message": error.message,
        "retry_after_seconds": error.retry_after_seconds,
    }


def _load_error(data: dict | None) -> ProviderError | None:
    if data is None:
        return None
    return ProviderError(
        code=data["code"],
        message=data["message"],
        retry_after_seconds=data.get("retry_after_seconds"),
    )


def _snapshot(snapshot: ExternalSnapshot) -> dict:
    return {
        "inn": snapshot.inn,
        "section": snapshot.section.value,
        "source": snapshot.source,
        "mode": snapshot.mode.value,
        "fetched_at": _moment(snapshot.fetched_at),
        "status": snapshot.status.value,
        "coverage": snapshot.coverage.value,
        "facts": [_fact(fact) for fact in snapshot.facts],
        "evidence": [_evidence(item) for item in snapshot.evidence],
        "missing": list(snapshot.missing),
        "covered_period": _period(snapshot.covered_period),
        "source_updated_at": _moment(snapshot.source_updated_at),
        "error": _error(snapshot.error),
    }


def _load_snapshot(data: dict) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn=data["inn"],
        section=Section(data["section"]),
        source=data["source"],
        mode=DataMode(data["mode"]),
        fetched_at=_load_moment(data["fetched_at"]),
        status=FetchStatus(data["status"]),
        coverage=Coverage(data["coverage"]),
        facts=tuple(_load_fact(item) for item in data["facts"]),
        evidence=tuple(_load_evidence(item) for item in data["evidence"]),
        missing=tuple(data["missing"]),
        covered_period=_load_period(data.get("covered_period")),
        source_updated_at=_load_moment(data.get("source_updated_at")),
        error=_load_error(data.get("error")),
    )


def dump_snapshots(snapshots: tuple[ExternalSnapshot, ...]) -> str:
    return json.dumps([_snapshot(snapshot) for snapshot in snapshots], ensure_ascii=False)


def load_snapshots(payload: str) -> tuple[ExternalSnapshot, ...]:
    try:
        return tuple(_load_snapshot(item) for item in json.loads(payload))
    except (ValueError, KeyError, TypeError) as exc:
        raise PayloadError("snapshot payload is not readable") from exc
