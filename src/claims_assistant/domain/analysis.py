"""Analysis run and uploaded file: the stored state of one check (S2-01 contract)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from .external import DataMode, Period


class RunStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


# docs/architecture.md: draft → queued → running → completed | partial | failed.
_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DRAFT: frozenset({RunStatus.QUEUED}),
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.PARTIAL: frozenset(),
    RunStatus.FAILED: frozenset(),
}


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in _TRANSITIONS[current]


class FileKind(StrEnum):
    """Sheets from docs/data-contracts.md; the user names the kind when uploading."""

    COUNTERPARTIES = "counterparties"
    PAYMENTS = "payments"
    INTERACTIONS = "interactions"
    DEBT_HISTORY = "debt_history"


def _utc(value: datetime, field: str) -> datetime:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def validate_checksum(checksum: str) -> None:
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError("Checksum must be a lowercase SHA-256 hex digest")


def validate_stored_path(path: str) -> None:
    parts = PurePosixPath(path)
    if not path or parts.is_absolute() or ".." in parts.parts or "\\" in path:
        raise ValueError("Stored path must be relative to the storage root and stay inside it")


@dataclass(frozen=True, slots=True)
class UploadedFile:
    id: str
    run_id: str
    kind: FileKind
    checksum: str
    size_bytes: int
    stored_path: str
    uploaded_at: datetime
    coverage: Period | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.run_id:
            raise ValueError("File and run identifiers must not be empty")
        validate_checksum(self.checksum)
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("File size must be a positive integer")
        validate_stored_path(self.stored_path)
        object.__setattr__(self, "uploaded_at", _utc(self.uploaded_at, "uploaded_at"))


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    id: str
    owner_id: int
    analysis_date: date
    mode: DataMode
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    files: tuple[UploadedFile, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Run identifier must not be empty")
        if type(self.owner_id) is not int or self.owner_id <= 0:
            raise ValueError("Owner must be a positive Telegram ID")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if any(file.run_id != self.id for file in self.files):
            raise ValueError("Every file must belong to this run")
