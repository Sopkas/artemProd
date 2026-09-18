"""Worker side of run storage (S2-02 contract): claim, finish, recover.

The queue is the same table as the runs; one background worker claims the oldest queued
run, processes it and records a final outcome. These calls are system-level, not
owner-scoped: the worker acts for every owner but never reads or exposes package data.
"""

from dataclasses import dataclass
from typing import Protocol

from claims_assistant.domain.analysis import FINAL_STATUSES, AnalysisRun, RunStatus

# A run interrupted this many times (process restarts mid-processing) is failed on
# recovery instead of being retried forever.
MAX_ATTEMPTS = 3
GAVE_UP = "Обработка прерывалась несколько раз подряд; проверка остановлена."


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    failure: str | None = None

    def __post_init__(self) -> None:
        if self.status not in FINAL_STATUSES:
            raise ValueError("Outcome must be completed, partial or failed")
        if self.failure is not None and self.status not in (RunStatus.FAILED, RunStatus.PARTIAL):
            raise ValueError("Failure text needs a failed or partial outcome")
        if self.failure is not None and not self.failure.strip():
            raise ValueError("Failure text must not be empty")


class AnalysisQueue(Protocol):
    async def claim_next(self) -> AnalysisRun | None:
        """Atomically move the oldest queued run to running (attempts + 1), or None."""
        ...

    async def finish(self, run_id: str, outcome: RunOutcome) -> AnalysisRun:
        """Record the final status; InvalidTransition unless the run is running."""
        ...

    async def recover_interrupted(self) -> tuple[AnalysisRun, ...]:
        """On startup: running runs go back to queued, or to failed after MAX_ATTEMPTS."""
        ...
