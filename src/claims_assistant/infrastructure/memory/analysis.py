"""Reference AnalysisRepository kept in process memory. SQLite replaces it in S2-01."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

from claims_assistant.application.analysis_queue import GAVE_UP, MAX_ATTEMPTS, RunOutcome
from claims_assistant.application.analysis_repository import (
    InvalidTransition,
    NewFile,
    RunLocked,
    RunNotFound,
)
from claims_assistant.domain.analysis import AnalysisRun, RunStatus, UploadedFile, can_transition
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.steps import DeliveryStatus, ReportArtifact, StepResult


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryAnalysisRepository:
    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._runs: dict[str, AnalysisRun] = {}
        # Creation order, the same tie-breaker the SQLite implementation stores as `sequence`.
        self._sequence: dict[str, int] = {}
        self._steps: dict[tuple[str, str, str, str], StepResult] = {}
        self._reports: dict[str, ReportArtifact] = {}

    async def create_run(self, owner_id: int, analysis_date: date, mode: DataMode) -> AnalysisRun:
        now = self._clock()
        run = AnalysisRun(
            id=uuid4().hex,
            owner_id=owner_id,
            analysis_date=analysis_date,
            mode=mode,
            status=RunStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._runs[run.id] = run
        self._sequence[run.id] = len(self._sequence) + 1
        return run

    async def get_run(self, owner_id: int, run_id: str) -> AnalysisRun:
        run = self._runs.get(run_id)
        if run is None or run.owner_id != owner_id:
            raise RunNotFound()
        return run

    async def list_runs(self, owner_id: int) -> tuple[AnalysisRun, ...]:
        mine = [run for run in self._runs.values() if run.owner_id == owner_id]
        return tuple(
            sorted(mine, key=lambda run: (run.created_at, self._sequence[run.id]), reverse=True)
        )

    async def add_file(self, owner_id: int, run_id: str, file: NewFile) -> UploadedFile:
        run = await self.get_run(owner_id, run_id)
        if run.status != RunStatus.DRAFT:
            raise RunLocked(run.status)
        for existing in run.files:
            if existing.kind == file.kind and existing.checksum == file.checksum:
                return existing
        now = self._clock()
        stored = UploadedFile(
            id=uuid4().hex,
            run_id=run.id,
            kind=file.kind,
            checksum=file.checksum,
            size_bytes=file.size_bytes,
            stored_path=file.stored_path,
            uploaded_at=now,
            coverage=file.coverage,
        )
        self._runs[run.id] = replace(run, files=run.files + (stored,), updated_at=now)
        return stored

    async def transition(self, owner_id: int, run_id: str, target: RunStatus) -> AnalysisRun:
        run = await self.get_run(owner_id, run_id)
        if not can_transition(run.status, target):
            raise InvalidTransition(run.status, target)
        updated = replace(run, status=target, updated_at=self._clock())
        self._runs[run.id] = updated
        return updated

    # --- AnalysisQueue ---

    async def claim_next(self) -> AnalysisRun | None:
        queued = [run for run in self._runs.values() if run.status == RunStatus.QUEUED]
        if not queued:
            return None
        run = min(queued, key=lambda item: (item.updated_at, self._sequence[item.id]))
        claimed = replace(
            run, status=RunStatus.RUNNING, attempts=run.attempts + 1, updated_at=self._clock()
        )
        self._runs[run.id] = claimed
        return claimed

    async def finish(self, run_id: str, outcome: RunOutcome) -> AnalysisRun:
        run = self._runs.get(run_id)
        if run is None:
            raise RunNotFound()
        if run.status != RunStatus.RUNNING:
            raise InvalidTransition(run.status, outcome.status)
        done = replace(
            run, status=outcome.status, failure=outcome.failure, updated_at=self._clock()
        )
        self._runs[run.id] = done
        return done

    async def recover_interrupted(self) -> tuple[AnalysisRun, ...]:
        recovered = []
        running = [run for run in self._runs.values() if run.status == RunStatus.RUNNING]
        for run in sorted(running, key=lambda item: self._sequence[item.id]):
            if run.attempts >= MAX_ATTEMPTS:
                updated = replace(
                    run, status=RunStatus.FAILED, failure=GAVE_UP, updated_at=self._clock()
                )
            else:
                updated = replace(run, status=RunStatus.QUEUED, updated_at=self._clock())
            self._runs[run.id] = updated
            recovered.append(updated)
        return tuple(recovered)

    # --- StepStore ---

    async def save_step(self, result: StepResult) -> StepResult:
        if result.run_id not in self._runs:
            raise RunNotFound()
        return self._steps.setdefault(result.key, result)

    async def get_step(self, run_id: str, inn: str, step: str, version: str) -> StepResult | None:
        return self._steps.get((run_id, inn, step, version))

    async def list_steps(self, run_id: str) -> tuple[StepResult, ...]:
        return tuple(item for item in self._steps.values() if item.run_id == run_id)

    # --- ReportStore ---

    async def save_report(self, run_id: str, stored_path: str) -> ReportArtifact:
        if run_id not in self._runs:
            raise RunNotFound()
        report = ReportArtifact(run_id=run_id, stored_path=stored_path, created_at=self._clock())
        self._reports[run_id] = report
        return report

    async def get_report(self, owner_id: int, run_id: str) -> ReportArtifact | None:
        await self.get_run(owner_id, run_id)
        return self._reports.get(run_id)

    async def mark_delivery(
        self, run_id: str, status: DeliveryStatus, error: str | None = None
    ) -> ReportArtifact:
        report = self._reports.get(run_id)
        if report is None:
            raise RunNotFound()
        delivered_at = self._clock() if status is DeliveryStatus.DELIVERED else None
        updated = replace(report, delivery=status, delivered_at=delivered_at, delivery_error=error)
        self._reports[run_id] = updated
        return updated
