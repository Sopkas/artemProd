"""Storage boundary for analysis runs (S2-01). Every call is scoped by the owner."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from claims_assistant.domain.analysis import (
    AnalysisRun,
    FileKind,
    RunStatus,
    UploadedFile,
    validate_checksum,
    validate_file_inn,
    validate_stored_path,
)
from claims_assistant.domain.external import DataMode, Period


class RunNotFound(LookupError):
    """Raised for a missing run and for another owner's run alike; never names the run."""

    def __init__(self) -> None:
        super().__init__("Проверка не найдена.")


class RunLocked(ValueError):
    """The input package is frozen once the run leaves the draft state."""

    def __init__(self, status: RunStatus) -> None:
        super().__init__(f"Нельзя добавить файл: проверка в состоянии {status}.")
        self.status = status


class InvalidTransition(ValueError):
    def __init__(self, current: RunStatus, requested: RunStatus) -> None:
        super().__init__(f"Переход {current} → {requested} не допускается.")
        self.current = current
        self.requested = requested


class RepositoryError(RuntimeError):
    """A storage failure. Implementations raise it instead of returning empty results."""


@dataclass(frozen=True, slots=True)
class NewFile:
    kind: FileKind
    checksum: str
    size_bytes: int
    stored_path: str
    coverage: Period | None = None
    inn: str | None = None  # whose 1C export this is; None: the whole package's

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FileKind):
            raise ValueError("File kind must be a FileKind")
        if self.coverage is not None and not isinstance(self.coverage, Period):
            raise ValueError("Coverage must be a Period or None")
        validate_file_inn(self.kind, self.inn)
        validate_checksum(self.checksum)
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("File size must be a positive integer")
        validate_stored_path(self.stored_path)


class AnalysisRepository(Protocol):
    """Returned objects are snapshots; later changes never mutate them.

    Missing runs and runs of other owners raise RunNotFound with the same message.
    A storage failure raises RepositoryError, never an empty success.
    """

    async def create_run(self, owner_id: int, analysis_date: date, mode: DataMode) -> AnalysisRun:
        """Create a draft run with no files."""
        ...

    async def get_run(self, owner_id: int, run_id: str) -> AnalysisRun: ...

    async def list_runs(self, owner_id: int) -> tuple[AnalysisRun, ...]:
        """Newest first."""
        ...

    async def add_file(self, owner_id: int, run_id: str, file: NewFile) -> UploadedFile:
        """Attach a file to a draft. The same (kind, checksum) returns the stored file
        unchanged; a run that left the draft state raises RunLocked."""
        ...

    async def transition(self, owner_id: int, run_id: str, target: RunStatus) -> AnalysisRun:
        """Apply one step of the state machine or raise InvalidTransition unchanged."""
        ...
