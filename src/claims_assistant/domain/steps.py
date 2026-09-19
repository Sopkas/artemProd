"""Saved step results and the report artifact of a run (S3-03 contract).

A step result is keyed by (run_id, inn, step, version); a completed step is never
executed again on resume. Report delivery is tracked apart from the analysis itself, so
a finished report can be sent again without a new check.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .analysis import validate_stored_path

# `inn` value for steps that belong to the whole run (e.g. the report), not one company.
RUN_SCOPE = ""
_STEP_NAME = re.compile(r"[a-z][a-z0-9_]*")
_INN = re.compile(r"[0-9]{10}")


class StepStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


def _utc(value: datetime, field: str) -> datetime:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class StepResult:
    run_id: str
    inn: str  # 10-digit INN or RUN_SCOPE
    step: str  # stable snake_case name, e.g. external_fetch, scoring, report
    version: str  # rules/adapter/prompt version the result was produced with
    status: StepStatus
    completed_at: datetime
    payload: str | None = None  # opaque to storage (JSON produced by the step)
    error: str | None = None  # safe text for failed steps

    def __post_init__(self) -> None:
        if not self.run_id or not self.version:
            raise ValueError("Step needs a run and a version")
        if self.inn != RUN_SCOPE and not _INN.fullmatch(self.inn):
            raise ValueError("Step scope must be a 10-digit INN or the run scope")
        if not _STEP_NAME.fullmatch(self.step):
            raise ValueError("Step name must be a stable snake_case identifier")
        if not isinstance(self.status, StepStatus):
            raise ValueError("Step status must be a StepStatus")
        if self.status is StepStatus.OK and self.error is not None:
            raise ValueError("A successful step has no error")
        if self.status is StepStatus.FAILED:
            if not self.error or not self.error.strip():
                raise ValueError("A failed step needs a safe error text")
            if self.payload is not None:
                raise ValueError("A failed step has no payload")
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.run_id, self.inn, self.step, self.version)


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    run_id: str
    stored_path: str
    created_at: datetime
    delivery: DeliveryStatus = DeliveryStatus.PENDING
    delivered_at: datetime | None = None
    delivery_error: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("Report needs a run")
        validate_stored_path(self.stored_path)
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if not isinstance(self.delivery, DeliveryStatus):
            raise ValueError("Delivery must be a DeliveryStatus")
        if self.delivery is DeliveryStatus.DELIVERED:
            if self.delivered_at is None or self.delivery_error is not None:
                raise ValueError("A delivered report has a delivery time and no error")
            object.__setattr__(self, "delivered_at", _utc(self.delivered_at, "delivered_at"))
        else:
            if self.delivered_at is not None:
                raise ValueError("Only a delivered report has a delivery time")
            if self.delivery is DeliveryStatus.FAILED and not (
                self.delivery_error and self.delivery_error.strip()
            ):
                raise ValueError("A failed delivery needs a safe error text")
            if self.delivery is DeliveryStatus.PENDING and self.delivery_error is not None:
                raise ValueError("A pending delivery has no error")
