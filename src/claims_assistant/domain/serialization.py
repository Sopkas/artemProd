"""JSON-compatible dicts for domain results kept as step payloads (S3-03).

``StepResult.payload`` is an opaque JSON string: the pipeline stores
``json.dumps(snapshot_to_dict(...), ensure_ascii=False)`` for ``external_fetch`` and
``assessment_to_dict(...)`` for ``scoring``, and reads them back with the ``*_from_dict``
pair. Serializing domain types belongs to B; storage never interprets the payload.

- Every top-level dict carries ``"schema"``; any other value is rejected, so a payload in an
  older format is never silently misread after the format changes.
- Fact values are tagged with their type, so a ``Decimal`` never comes back as a float and
  ``True`` never as ``1``.
- Reading goes through the ordinary constructors: every domain check applies again.
- Errors never echo the payload, which may hold INNs or provider text.
"""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

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
from claims_assistant.domain.scoring import Assessment, Priority, Signal

SCHEMA = 1


class PayloadError(ValueError):
    """A stored payload does not match the expected format; the payload is not echoed."""


def _field(data: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if not isinstance(data, Mapping) or key not in data:
        raise PayloadError(f"Missing field: {key}")
    value = data[key]
    kinds = kind if isinstance(kind, tuple) else (kind,)
    # A bool is also an int in Python; accept it only where a bool is expected.
    if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
        raise PayloadError(f"Unexpected type for field: {key}")
    return value


def _optional(data: Mapping[str, Any], key: str, read: Callable[[Any], Any]) -> Any:
    value = data.get(key) if isinstance(data, Mapping) else None
    return None if value is None else read(value)


def _enum(enum: type, value: Any, key: str) -> Any:
    try:
        return enum(value)
    except ValueError:
        raise PayloadError(f"Unknown value for field: {key}") from None


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise PayloadError("A date must be an ISO string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PayloadError("A date must be an ISO string") from None


def _moment(value: Any) -> datetime:
    if not isinstance(value, str):
        raise PayloadError("A timestamp must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PayloadError("A timestamp must be an ISO string") from None
    if parsed.utcoffset() is None:
        raise PayloadError("A timestamp must include a timezone")
    return parsed


def _check_schema(data: Any) -> None:
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
        raise PayloadError("Unsupported payload schema")


def _built(factory: Callable[[], Any]) -> Any:
    """Run a domain constructor; its validation errors become payload errors."""
    try:
        return factory()
    except (TypeError, ValueError) as error:
        if isinstance(error, PayloadError):
            raise
        raise PayloadError("Payload violates the domain rules") from None


# --- fact values ---


def _value_to_dict(value: object) -> dict[str, Any]:
    if value is None:
        return {"type": "none", "value": None}
    if isinstance(value, CompanyStatus):  # before str: a StrEnum is also a str
        return {"type": "company_status", "value": value.value}
    if isinstance(value, bool):  # before int: a bool is also an int
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    raise PayloadError("Unsupported fact value type")


def _value_from_dict(data: Any) -> object:
    kind = _field(data, "type", str)
    raw = data.get("value")
    if kind == "none" and raw is None:
        return None
    if kind == "company_status" and isinstance(raw, str):
        return _enum(CompanyStatus, raw, "value")
    if kind == "bool" and isinstance(raw, bool):
        return raw
    if kind == "int" and isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if kind == "decimal" and isinstance(raw, str):
        try:
            return Decimal(raw)
        except InvalidOperation:
            raise PayloadError("Unreadable decimal value") from None
    if kind == "date":
        return _day(raw)
    if kind == "str" and isinstance(raw, str):
        return raw
    raise PayloadError("Unsupported fact value type")


# --- snapshot parts ---


def _period_to_dict(period: Period | None) -> dict[str, str] | None:
    if period is None:
        return None
    return {"start": period.start.isoformat(), "end": period.end.isoformat()}


def _period_from_dict(data: Any) -> Period:
    start, end = _day(_field(data, "start", str)), _day(_field(data, "end", str))
    return _built(lambda: Period(start, end))


def _fact_to_dict(fact: Fact) -> dict[str, Any]:
    return {
        "id": fact.id,
        "inn": fact.inn,
        "kind": fact.kind.value,
        "value": _value_to_dict(fact.value),
        "evidence_ids": list(fact.evidence_ids),
        "observed_on": fact.observed_on.isoformat() if fact.observed_on else None,
        "period": _period_to_dict(fact.period),
        "unit": fact.unit,
        "missing_reason": fact.missing_reason,
    }


def _fact_from_dict(data: Any) -> Fact:
    evidence_ids = _field(data, "evidence_ids", list)
    if not all(isinstance(item, str) for item in evidence_ids):
        raise PayloadError("Unexpected type for field: evidence_ids")
    fields = dict(
        id=_field(data, "id", str),
        inn=_field(data, "inn", str),
        kind=_enum(FactKind, _field(data, "kind", str), "kind"),
        value=_value_from_dict(_field(data, "value", Mapping)),
        evidence_ids=tuple(evidence_ids),
        observed_on=_optional(data, "observed_on", _day),
        period=_optional(data, "period", _period_from_dict),
        unit=_optional(data, "unit", lambda v: _field({"unit": v}, "unit", str)),
        missing_reason=_optional(
            data, "missing_reason", lambda v: _field({"reason": v}, "reason", str)
        ),
    )
    return _built(lambda: Fact(**fields))


def _evidence_to_dict(item: Evidence) -> dict[str, Any]:
    return {"id": item.id, "source": item.source, "record_id": item.record_id, "url": item.url}


def _evidence_from_dict(data: Any) -> Evidence:
    return Evidence(
        _field(data, "id", str),
        _field(data, "source", str),
        _field(data, "record_id", str),
        _optional(data, "url", lambda v: _field({"url": v}, "url", str)),
    )


def _error_to_dict(error: ProviderError | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {
        "code": error.code,
        "message": error.message,
        "retry_after_seconds": error.retry_after_seconds,
    }


def _error_from_dict(data: Any) -> ProviderError:
    retry = _optional(
        data, "retry_after_seconds", lambda v: _field({"retry": v}, "retry", (int, float))
    )
    code, message = _field(data, "code", str), _field(data, "message", str)
    return _built(lambda: ProviderError(code, message, retry))


# --- public API ---


def snapshot_to_dict(snapshot: ExternalSnapshot) -> dict[str, Any]:
    """Payload of the ``external_fetch`` step for one section of one INN."""
    return {
        "schema": SCHEMA,
        "inn": snapshot.inn,
        "section": snapshot.section.value,
        "source": snapshot.source,
        "mode": snapshot.mode.value,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "status": snapshot.status.value,
        "coverage": snapshot.coverage.value,
        "facts": [_fact_to_dict(fact) for fact in snapshot.facts],
        "evidence": [_evidence_to_dict(item) for item in snapshot.evidence],
        "missing": list(snapshot.missing),
        "covered_period": _period_to_dict(snapshot.covered_period),
        "source_updated_at": (
            snapshot.source_updated_at.isoformat() if snapshot.source_updated_at else None
        ),
        "error": _error_to_dict(snapshot.error),
    }


def snapshot_from_dict(data: Any) -> ExternalSnapshot:
    _check_schema(data)
    missing = _field(data, "missing", list)
    if not all(isinstance(item, str) for item in missing):
        raise PayloadError("Unexpected type for field: missing")
    fields = dict(
        inn=_field(data, "inn", str),
        section=_enum(Section, _field(data, "section", str), "section"),
        source=_field(data, "source", str),
        mode=_enum(DataMode, _field(data, "mode", str), "mode"),
        fetched_at=_moment(_field(data, "fetched_at", str)),
        status=_enum(FetchStatus, _field(data, "status", str), "status"),
        coverage=_enum(Coverage, _field(data, "coverage", str), "coverage"),
        facts=tuple(_fact_from_dict(item) for item in _field(data, "facts", list)),
        evidence=tuple(_evidence_from_dict(item) for item in _field(data, "evidence", list)),
        missing=tuple(missing),
        covered_period=_optional(data, "covered_period", _period_from_dict),
        source_updated_at=_optional(data, "source_updated_at", _moment),
        error=_optional(data, "error", _error_from_dict),
    )
    return _built(lambda: ExternalSnapshot(**fields))


def signal_to_dict(signal: Signal) -> dict[str, Any]:
    return {
        "code": signal.code,
        "level": signal.level.value,
        "reason": signal.reason,
        "fact_ids": list(signal.fact_ids),
        "value": signal.value,
        "observed_on": signal.observed_on.isoformat() if signal.observed_on else None,
    }


def signal_from_dict(data: Any) -> Signal:
    fact_ids = _field(data, "fact_ids", list)
    if not all(isinstance(item, str) for item in fact_ids):
        raise PayloadError("Unexpected type for field: fact_ids")
    fields = dict(
        code=_field(data, "code", str),
        level=_enum(Priority, _field(data, "level", str), "level"),
        reason=_field(data, "reason", str),
        fact_ids=tuple(fact_ids),
        value=_optional(data, "value", lambda v: _field({"value": v}, "value", str)),
        observed_on=_optional(data, "observed_on", _day),
    )
    return _built(lambda: Signal(**fields))


def assessment_to_dict(assessment: Assessment) -> dict[str, Any]:
    """Payload of the ``scoring`` step for one INN."""
    return {
        "schema": SCHEMA,
        "inn": assessment.inn,
        "priority": assessment.priority.value,
        "signals": [signal_to_dict(signal) for signal in assessment.signals],
        "missing_data": list(assessment.missing_data),
        "coverage": {part: value.value for part, value in sorted(assessment.coverage.items())},
        "base_complete": assessment.base_complete,
        "rules_version": assessment.rules_version,
    }


def assessment_from_dict(data: Any) -> Assessment:
    _check_schema(data)
    missing = _field(data, "missing_data", list)
    if not all(isinstance(item, str) for item in missing):
        raise PayloadError("Unexpected type for field: missing_data")
    coverage = _field(data, "coverage", Mapping)
    if not all(isinstance(part, str) for part in coverage):
        raise PayloadError("Unexpected type for field: coverage")
    fields = dict(
        inn=_field(data, "inn", str),
        priority=_enum(Priority, _field(data, "priority", str), "priority"),
        signals=tuple(signal_from_dict(item) for item in _field(data, "signals", list)),
        missing_data=tuple(missing),
        coverage={part: _enum(Coverage, value, "coverage") for part, value in coverage.items()},
        base_complete=_field(data, "base_complete", bool),
        rules_version=_field(data, "rules_version", str),
    )
    return _built(lambda: Assessment(**fields))
