"""SQLite implementation of AnalysisRepository (S2-01).

Blocking database work runs in a worker thread so the Telegram loop is never blocked.
Every method is owner-scoped; storage failures raise RepositoryError instead of
returning an empty result. Migrations run on open, so a fresh file is ready to use.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event, func, select
from sqlalchemy.exc import SQLAlchemyError

from claims_assistant.application.analysis_queue import GAVE_UP, MAX_ATTEMPTS, RunOutcome
from claims_assistant.application.analysis_repository import (
    InvalidTransition,
    NewFile,
    RepositoryError,
    RunLocked,
    RunNotFound,
)
from claims_assistant.domain.analysis import (
    AnalysisRun,
    FileKind,
    RunStatus,
    UploadedFile,
    can_transition,
)
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.domain.steps import DeliveryStatus, ReportArtifact, StepResult, StepStatus

from .schema import analysis_runs, report_artifacts, run_steps, uploaded_files

_MIGRATIONS = Path(__file__).with_name("migrations")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _unstamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def open_sqlite_repository(
    path: Path, clock: Callable[[], datetime] = _now
) -> "SqliteAnalysisRepository":
    """Create the file and its parent directory if needed and upgrade the schema."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{path.as_posix()}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            _upgrade(connection)
    except SQLAlchemyError as error:
        engine.dispose()
        raise RepositoryError("Не удалось подготовить базу данных.") from error
    return SqliteAnalysisRepository(engine, clock)


def _upgrade(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS.as_posix())
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


class SqliteAnalysisRepository:
    def __init__(self, engine: Engine, clock: Callable[[], datetime] = _now) -> None:
        self._engine = engine
        self._clock = clock
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._engine.dispose()

    async def create_run(self, owner_id: int, analysis_date: date, mode: DataMode) -> AnalysisRun:
        return await self._run(self._create_run, owner_id, analysis_date, mode)

    async def get_run(self, owner_id: int, run_id: str) -> AnalysisRun:
        return await self._run(self._get_run, owner_id, run_id)

    async def list_runs(self, owner_id: int) -> tuple[AnalysisRun, ...]:
        return await self._run(self._list_runs, owner_id)

    async def add_file(self, owner_id: int, run_id: str, file: NewFile) -> UploadedFile:
        return await self._run(self._add_file, owner_id, run_id, file)

    async def transition(self, owner_id: int, run_id: str, target: RunStatus) -> AnalysisRun:
        return await self._run(self._transition, owner_id, run_id, target)

    async def claim_next(self) -> AnalysisRun | None:
        return await self._run(self._claim_next)

    async def finish(self, run_id: str, outcome: RunOutcome) -> AnalysisRun:
        return await self._run(self._finish, run_id, outcome)

    async def recover_interrupted(self) -> tuple[AnalysisRun, ...]:
        return await self._run(self._recover_interrupted)

    async def save_step(self, result: StepResult) -> StepResult:
        return await self._run(self._save_step, result)

    async def get_step(self, run_id: str, inn: str, step: str, version: str) -> StepResult | None:
        return await self._run(self._get_step, run_id, inn, step, version)

    async def list_steps(self, run_id: str) -> tuple[StepResult, ...]:
        return await self._run(self._list_steps, run_id)

    async def save_report(self, run_id: str, stored_path: str) -> ReportArtifact:
        return await self._run(self._save_report, run_id, stored_path)

    async def get_report(self, owner_id: int, run_id: str) -> ReportArtifact | None:
        return await self._run(self._get_report, owner_id, run_id)

    async def mark_delivery(
        self, run_id: str, status: DeliveryStatus, error: str | None = None
    ) -> ReportArtifact:
        return await self._run(self._mark_delivery, run_id, status, error)

    async def _run(self, operation, *args):
        if self._closed:
            raise RepositoryError("Хранилище закрыто.")
        try:
            return await asyncio.to_thread(operation, *args)
        except SQLAlchemyError as error:
            raise RepositoryError("Сбой хранилища проверок.") from error

    # --- blocking work, executed in a worker thread ---

    def _create_run(self, owner_id: int, analysis_date: date, mode: DataMode) -> AnalysisRun:
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
        with self._engine.begin() as connection:
            sequence = connection.execute(
                select(func.coalesce(func.max(analysis_runs.c.sequence), 0) + 1)
            ).scalar_one()
            connection.execute(
                analysis_runs.insert().values(
                    id=run.id,
                    owner_id=run.owner_id,
                    analysis_date=run.analysis_date.isoformat(),
                    mode=run.mode.value,
                    status=run.status.value,
                    created_at=_stamp(run.created_at),
                    updated_at=_stamp(run.updated_at),
                    sequence=sequence,
                )
            )
        return run

    def _get_run(self, owner_id: int, run_id: str) -> AnalysisRun:
        with self._engine.connect() as connection:
            return self._load_run(connection, owner_id, run_id)

    def _list_runs(self, owner_id: int) -> tuple[AnalysisRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(analysis_runs)
                .where(analysis_runs.c.owner_id == owner_id)
                .order_by(analysis_runs.c.created_at.desc(), analysis_runs.c.sequence.desc())
            ).all()
            return tuple(self._to_run(connection, row) for row in rows)

    def _add_file(self, owner_id: int, run_id: str, file: NewFile) -> UploadedFile:
        with self._engine.begin() as connection:
            run = self._load_run(connection, owner_id, run_id)
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
            sequence = connection.execute(
                select(func.coalesce(func.max(uploaded_files.c.sequence), 0) + 1).where(
                    uploaded_files.c.run_id == run.id
                )
            ).scalar_one()
            connection.execute(
                uploaded_files.insert().values(
                    id=stored.id,
                    run_id=stored.run_id,
                    kind=stored.kind.value,
                    checksum=stored.checksum,
                    size_bytes=stored.size_bytes,
                    stored_path=stored.stored_path,
                    uploaded_at=_stamp(stored.uploaded_at),
                    coverage_start=None
                    if file.coverage is None
                    else file.coverage.start.isoformat(),
                    coverage_end=None if file.coverage is None else file.coverage.end.isoformat(),
                    sequence=sequence,
                )
            )
            connection.execute(
                analysis_runs.update()
                .where(analysis_runs.c.id == run.id)
                .values(updated_at=_stamp(now))
            )
            return stored

    def _transition(self, owner_id: int, run_id: str, target: RunStatus) -> AnalysisRun:
        with self._engine.begin() as connection:
            run = self._load_run(connection, owner_id, run_id)
            if not can_transition(run.status, target):
                raise InvalidTransition(run.status, target)
            now = self._clock()
            connection.execute(
                analysis_runs.update()
                .where(analysis_runs.c.id == run.id, analysis_runs.c.status == run.status.value)
                .values(status=target.value, updated_at=_stamp(now))
            )
            return self._load_run(connection, owner_id, run_id)

    def _claim_next(self) -> AnalysisRun | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(analysis_runs.c.id)
                .where(analysis_runs.c.status == RunStatus.QUEUED.value)
                .order_by(analysis_runs.c.updated_at, analysis_runs.c.sequence)
                .limit(1)
            ).first()
            if row is None:
                return None
            # The status guard makes the claim atomic even with a second worker.
            claimed = connection.execute(
                analysis_runs.update()
                .where(
                    analysis_runs.c.id == row.id,
                    analysis_runs.c.status == RunStatus.QUEUED.value,
                )
                .values(
                    status=RunStatus.RUNNING.value,
                    attempts=analysis_runs.c.attempts + 1,
                    updated_at=_stamp(self._clock()),
                )
            ).rowcount
            if claimed != 1:
                return None
            return self._to_run(connection, self._row_by_id(connection, row.id))

    def _finish(self, run_id: str, outcome: RunOutcome) -> AnalysisRun:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(analysis_runs).where(analysis_runs.c.id == run_id)
            ).one_or_none()
            if row is None:
                raise RunNotFound()
            if row.status != RunStatus.RUNNING.value:
                raise InvalidTransition(RunStatus(row.status), outcome.status)
            connection.execute(
                analysis_runs.update()
                .where(analysis_runs.c.id == run_id)
                .values(
                    status=outcome.status.value,
                    failure=outcome.failure,
                    updated_at=_stamp(self._clock()),
                )
            )
            return self._to_run(connection, self._row_by_id(connection, run_id))

    def _recover_interrupted(self) -> tuple[AnalysisRun, ...]:
        recovered = []
        with self._engine.begin() as connection:
            rows = connection.execute(
                select(analysis_runs)
                .where(analysis_runs.c.status == RunStatus.RUNNING.value)
                .order_by(analysis_runs.c.sequence)
            ).all()
            for row in rows:
                if row.attempts >= MAX_ATTEMPTS:
                    values = {"status": RunStatus.FAILED.value, "failure": GAVE_UP}
                else:
                    values = {"status": RunStatus.QUEUED.value}
                connection.execute(
                    analysis_runs.update()
                    .where(analysis_runs.c.id == row.id)
                    .values(updated_at=_stamp(self._clock()), **values)
                )
                recovered.append(self._to_run(connection, self._row_by_id(connection, row.id)))
        return tuple(recovered)

    # --- steps and reports ---

    def _run_exists(self, connection: Connection, run_id: str) -> bool:
        query = select(analysis_runs.c.id).where(analysis_runs.c.id == run_id)
        return connection.execute(query).first() is not None

    def _save_step(self, result: StepResult) -> StepResult:
        with self._engine.begin() as connection:
            if not self._run_exists(connection, result.run_id):
                raise RunNotFound()
            existing = self._step_row(connection, *result.key)
            if existing is not None:
                return _to_step(existing)
            sequence = connection.execute(
                select(func.coalesce(func.max(run_steps.c.sequence), 0) + 1).where(
                    run_steps.c.run_id == result.run_id
                )
            ).scalar_one()
            connection.execute(
                run_steps.insert().values(
                    run_id=result.run_id,
                    inn=result.inn,
                    step=result.step,
                    version=result.version,
                    status=result.status.value,
                    payload=result.payload,
                    error=result.error,
                    completed_at=_stamp(result.completed_at),
                    sequence=sequence,
                )
            )
            return result

    def _get_step(self, run_id: str, inn: str, step: str, version: str) -> StepResult | None:
        with self._engine.connect() as connection:
            row = self._step_row(connection, run_id, inn, step, version)
            return None if row is None else _to_step(row)

    def _list_steps(self, run_id: str) -> tuple[StepResult, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(run_steps).where(run_steps.c.run_id == run_id).order_by(run_steps.c.sequence)
            ).all()
            return tuple(_to_step(row) for row in rows)

    def _step_row(self, connection: Connection, run_id: str, inn: str, step: str, version: str):
        return connection.execute(
            select(run_steps).where(
                run_steps.c.run_id == run_id,
                run_steps.c.inn == inn,
                run_steps.c.step == step,
                run_steps.c.version == version,
            )
        ).one_or_none()

    def _save_report(self, run_id: str, stored_path: str) -> ReportArtifact:
        with self._engine.begin() as connection:
            if not self._run_exists(connection, run_id):
                raise RunNotFound()
            report = ReportArtifact(
                run_id=run_id, stored_path=stored_path, created_at=self._clock()
            )
            connection.execute(report_artifacts.delete().where(report_artifacts.c.run_id == run_id))
            connection.execute(
                report_artifacts.insert().values(
                    run_id=run_id,
                    stored_path=report.stored_path,
                    created_at=_stamp(report.created_at),
                    delivery=report.delivery.value,
                    delivered_at=None,
                    delivery_error=None,
                )
            )
            return report

    def _get_report(self, owner_id: int, run_id: str) -> ReportArtifact | None:
        with self._engine.connect() as connection:
            self._load_run(connection, owner_id, run_id)
            row = connection.execute(
                select(report_artifacts).where(report_artifacts.c.run_id == run_id)
            ).one_or_none()
            return None if row is None else _to_report(row)

    def _mark_delivery(
        self, run_id: str, status: DeliveryStatus, error: str | None
    ) -> ReportArtifact:
        with self._engine.begin() as connection:
            row = connection.execute(
                select(report_artifacts).where(report_artifacts.c.run_id == run_id)
            ).one_or_none()
            if row is None:
                raise RunNotFound()
            delivered_at = self._clock() if status is DeliveryStatus.DELIVERED else None
            updated = ReportArtifact(
                run_id=run_id,
                stored_path=row.stored_path,
                created_at=_unstamp(row.created_at),
                delivery=status,
                delivered_at=delivered_at,
                delivery_error=error,
            )
            connection.execute(
                report_artifacts.update()
                .where(report_artifacts.c.run_id == run_id)
                .values(
                    delivery=status.value,
                    delivered_at=None if delivered_at is None else _stamp(updated.delivered_at),
                    delivery_error=error,
                )
            )
            return updated

    def _row_by_id(self, connection: Connection, run_id: str):
        return connection.execute(select(analysis_runs).where(analysis_runs.c.id == run_id)).one()

    def _load_run(self, connection: Connection, owner_id: int, run_id: str) -> AnalysisRun:
        row = connection.execute(
            select(analysis_runs).where(
                analysis_runs.c.id == run_id, analysis_runs.c.owner_id == owner_id
            )
        ).one_or_none()
        if row is None:
            raise RunNotFound()
        return self._to_run(connection, row)

    def _to_run(self, connection: Connection, row) -> AnalysisRun:
        file_rows = connection.execute(
            select(uploaded_files)
            .where(uploaded_files.c.run_id == row.id)
            .order_by(uploaded_files.c.sequence)
        ).all()
        files = tuple(
            UploadedFile(
                id=item.id,
                run_id=item.run_id,
                kind=FileKind(item.kind),
                checksum=item.checksum,
                size_bytes=item.size_bytes,
                stored_path=item.stored_path,
                uploaded_at=_unstamp(item.uploaded_at),
                coverage=None
                if item.coverage_start is None
                else Period(
                    date.fromisoformat(item.coverage_start), date.fromisoformat(item.coverage_end)
                ),
            )
            for item in file_rows
        )
        return AnalysisRun(
            id=row.id,
            owner_id=row.owner_id,
            analysis_date=date.fromisoformat(row.analysis_date),
            mode=DataMode(row.mode),
            status=RunStatus(row.status),
            created_at=_unstamp(row.created_at),
            updated_at=_unstamp(row.updated_at),
            files=files,
            attempts=row.attempts,
            failure=row.failure,
        )


def _to_step(row) -> StepResult:
    return StepResult(
        run_id=row.run_id,
        inn=row.inn,
        step=row.step,
        version=row.version,
        status=StepStatus(row.status),
        completed_at=_unstamp(row.completed_at),
        payload=row.payload,
        error=row.error,
    )


def _to_report(row) -> ReportArtifact:
    return ReportArtifact(
        run_id=row.run_id,
        stored_path=row.stored_path,
        created_at=_unstamp(row.created_at),
        delivery=DeliveryStatus(row.delivery),
        delivered_at=None if row.delivered_at is None else _unstamp(row.delivered_at),
        delivery_error=row.delivery_error,
    )
