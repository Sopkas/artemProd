"""Reference AnalysisRepository kept in process memory. SQLite replaces it in S2-01."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

from claims_assistant.application.analysis_repository import (
    InvalidTransition,
    NewFile,
    RunLocked,
    RunNotFound,
)
from claims_assistant.domain.analysis import AnalysisRun, RunStatus, UploadedFile, can_transition
from claims_assistant.domain.external import DataMode


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryAnalysisRepository:
    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._runs: dict[str, AnalysisRun] = {}

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
        return run

    async def get_run(self, owner_id: int, run_id: str) -> AnalysisRun:
        run = self._runs.get(run_id)
        if run is None or run.owner_id != owner_id:
            raise RunNotFound()
        return run

    async def list_runs(self, owner_id: int) -> tuple[AnalysisRun, ...]:
        mine = [run for run in self._runs.values() if run.owner_id == owner_id]
        return tuple(sorted(mine, key=lambda run: run.created_at, reverse=True))

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
