"""Contract tests for AnalysisRepository. Every implementation must pass them unchanged."""

from datetime import UTC, date, datetime, timedelta

import pytest

from claims_assistant.application.analysis_repository import (
    AnalysisRepository,
    InvalidTransition,
    NewFile,
    RunLocked,
    RunNotFound,
)
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository

OWNER = 42
STRANGER = 99
DAY = date(2026, 9, 17)
START = datetime(2026, 9, 17, 10, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture(params=["memory"])
def repository(request) -> AnalysisRepository:
    if request.param == "memory":
        return InMemoryAnalysisRepository(clock=Clock())
    raise AssertionError(request.param)


def new_file(kind: FileKind = FileKind.COUNTERPARTIES, checksum: str = "a" * 64) -> NewFile:
    return NewFile(kind=kind, checksum=checksum, size_bytes=10, stored_path=f"{kind}.xlsx")


async def test_created_run_is_a_draft_owned_by_the_caller(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    assert run.status == RunStatus.DRAFT
    assert run.owner_id == OWNER
    assert run.analysis_date == DAY
    assert run.mode == DataMode.DEMO
    assert run.files == ()
    assert run.created_at == run.updated_at
    assert run.created_at.tzinfo is UTC
    assert await repository.get_run(OWNER, run.id) == run


async def test_run_ids_are_unique_and_listing_is_newest_first(repository):
    first = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    second = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    assert first.id != second.id
    assert [run.id for run in await repository.list_runs(OWNER)] == [second.id, first.id]


async def test_foreign_and_missing_runs_are_indistinguishable(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    with pytest.raises(RunNotFound) as foreign:
        await repository.get_run(STRANGER, run.id)
    with pytest.raises(RunNotFound) as missing:
        await repository.get_run(OWNER, "no-such-run")
    assert str(foreign.value) == str(missing.value)
    assert run.id not in str(foreign.value)
    assert await repository.list_runs(STRANGER) == ()


async def test_add_file_attaches_it_to_the_draft(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    period = Period(date(2026, 1, 1), date(2026, 6, 30))
    stored = await repository.add_file(
        OWNER,
        run.id,
        NewFile(FileKind.PAYMENTS, "b" * 64, 20, "payments.xlsx", coverage=period),
    )
    assert stored.run_id == run.id
    assert stored.kind == FileKind.PAYMENTS
    assert stored.coverage == period
    assert stored.uploaded_at.tzinfo is UTC
    reloaded = await repository.get_run(OWNER, run.id)
    assert reloaded.files == (stored,)
    assert reloaded.updated_at >= stored.uploaded_at


async def test_same_file_twice_is_not_duplicated(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    first = await repository.add_file(OWNER, run.id, new_file())
    second = await repository.add_file(OWNER, run.id, new_file())
    assert first == second
    assert len((await repository.get_run(OWNER, run.id)).files) == 1


async def test_same_content_as_a_different_kind_is_a_separate_file(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.add_file(OWNER, run.id, new_file(FileKind.COUNTERPARTIES))
    await repository.add_file(OWNER, run.id, new_file(FileKind.DEBT_HISTORY))
    assert len((await repository.get_run(OWNER, run.id)).files) == 2


async def test_files_are_locked_once_the_run_is_queued(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.add_file(OWNER, run.id, new_file())
    await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    with pytest.raises(RunLocked):
        await repository.add_file(OWNER, run.id, new_file(checksum="c" * 64))
    assert len((await repository.get_run(OWNER, run.id)).files) == 1


async def test_add_file_to_foreign_run_is_not_found(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    with pytest.raises(RunNotFound):
        await repository.add_file(STRANGER, run.id, new_file())
    assert (await repository.get_run(OWNER, run.id)).files == ()


async def test_transition_follows_the_state_machine_and_bumps_updated_at(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    queued = await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    assert queued.status == RunStatus.QUEUED
    assert queued.updated_at > run.updated_at
    running = await repository.transition(OWNER, run.id, RunStatus.RUNNING)
    done = await repository.transition(OWNER, run.id, RunStatus.PARTIAL)
    assert running.status == RunStatus.RUNNING and done.status == RunStatus.PARTIAL
    assert (await repository.get_run(OWNER, run.id)).status == RunStatus.PARTIAL


async def test_invalid_transition_is_explicit_and_changes_nothing(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    with pytest.raises(InvalidTransition) as info:
        await repository.transition(OWNER, run.id, RunStatus.COMPLETED)
    assert info.value.current == RunStatus.DRAFT
    assert info.value.requested == RunStatus.COMPLETED
    assert await repository.get_run(OWNER, run.id) == run


async def test_transition_of_foreign_run_is_not_found(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    with pytest.raises(RunNotFound):
        await repository.transition(STRANGER, run.id, RunStatus.QUEUED)
    assert (await repository.get_run(OWNER, run.id)).status == RunStatus.DRAFT


async def test_returned_runs_are_snapshots_not_live_objects(repository):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    assert run.status == RunStatus.DRAFT


@pytest.mark.parametrize(
    "bad",
    [
        {"checksum": "A" * 64},
        {"size_bytes": 0},
        {"stored_path": "../x.xlsx"},
    ],
)
def test_new_file_validates_like_uploaded_file(bad):
    fields = dict(kind=FileKind.COUNTERPARTIES, checksum="a" * 64, size_bytes=1, stored_path="x")
    fields.update(bad)
    with pytest.raises(ValueError):
        NewFile(**fields)
