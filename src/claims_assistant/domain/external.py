"""Minimal external-data contract for the single-company card (S1-04)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum


class Section(StrEnum):
    COMPANY = "company"
    BANKRUPTCY = "bankruptcy"
    FINANCES = "finances"


class DataMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class FetchStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    INVALID_RESPONSE = "invalid_response"


class Coverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("Period start must not follow its end")


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    source: str
    record_id: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    inn: str
    kind: str
    value: str | int | Decimal | bool | date | None
    evidence_ids: tuple[str, ...]
    observed_on: date | None = None
    period: Period | None = None
    unit: str | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        if self.value is not None and type(self.value) not in (str, int, Decimal, bool, date):
            raise TypeError("Fact value must use an exact supported type; floats are not allowed")
        if isinstance(self.value, Decimal) and not self.value.is_finite():
            raise ValueError("Decimal fact value must be finite")
        if self.value is None and not self.missing_reason:
            raise ValueError("Unknown fact value requires a reason")
        if self.value is not None and self.missing_reason is not None:
            raise ValueError("A known fact value cannot have a missing reason")
        if not self.evidence_ids:
            raise ValueError("Fact requires evidence")
        if self.observed_on is None and self.period is None:
            raise ValueError("Fact requires a date or period")


@dataclass(frozen=True, slots=True)
class ProviderError:
    """Safe code and summary, never an HTTP body, URL with a key or raw exception."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ExternalSnapshot:
    inn: str
    section: Section
    source: str
    mode: DataMode
    fetched_at: datetime
    status: FetchStatus
    coverage: Coverage
    facts: tuple[Fact, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    missing: tuple[str, ...] = ()
    covered_period: Period | None = None
    source_updated_at: datetime | None = None
    error: ProviderError | None = None

    def __post_init__(self) -> None:
        for field in ("fetched_at", "source_updated_at"):
            value = getattr(self, field)
            if value is not None:
                if value.utcoffset() is None:
                    raise ValueError("Technical timestamps must include a timezone")
                object.__setattr__(self, field, value.astimezone(UTC))
        if self.coverage == Coverage.PARTIAL and not self.missing:
            raise ValueError("Partial coverage requires a reason")
        if self.coverage == Coverage.COMPLETE and self.missing:
            raise ValueError("Complete coverage cannot contain missing data")
        if self.status != FetchStatus.OK:
            if self.coverage != Coverage.PARTIAL or self.error is None:
                raise ValueError("Failed section requires partial coverage and a safe error")
            if self.facts or self.evidence:
                raise ValueError("Failed section must not contain successful facts")
        elif self.error is not None:
            raise ValueError("Successful section cannot contain an error")
        evidence_ids = {item.id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("Duplicate evidence ID")
        if len({fact.id for fact in self.facts}) != len(self.facts):
            raise ValueError("Duplicate fact ID")
        for fact in self.facts:
            if fact.inn != self.inn or not set(fact.evidence_ids) <= evidence_ids:
                raise ValueError("Fact must match the snapshot INN and reference its evidence")
        if any(item.source != self.source for item in self.evidence):
            raise ValueError("Evidence must match the snapshot source")
