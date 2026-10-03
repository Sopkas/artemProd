"""Contract tests for AnalysisRepository. Every implementation must pass them unchanged."""

from datetime import UTC, date, datetime, timedelta

import pytest

from claims_assistant.application.analysis_queue import MAX_ATTEMPTS, RunOutcome
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
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository

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


@pytest.fixture(params=["memory", "sqlite"])
def repository(request, tmp_path) -> AnalysisRepository:
    if request.param == "memory":
        return InMemoryAnalysisRepository(clock=Clock())
    if request.param == "sqlite":
        return open_sqlite_repository(tmp_path / "claims.sqlite3", clock=Clock())
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


async def test_a_1c_export_remembers_whose_it_is(repository):
    """A 1C export is one company's: its period vouches for that company only (block 5
    of the manual test, 30.09.2026). A file of our template has no owner company."""
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    export = await repository.add_file(
        OWNER, run.id, NewFile(FileKind.PAYMENTS, "b" * 64, 20, "p1.xlsx", inn="7707083893")
    )
    template = await repository.add_file(
        OWNER, run.id, NewFile(FileKind.PAYMENTS, "c" * 64, 20, "p2.xlsx")
    )
    reloaded = await repository.get_run(OWNER, run.id)
    assert [file.inn for file in reloaded.files] == ["7707083893", None]
    assert (export.inn, template.inn) == ("7707083893", None)


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
        {"inn": "7707083893"},  # only payments have an owner company
        {"kind": FileKind.PAYMENTS, "inn": "7707083894"},  # a broken INN
    ],
)
def test_new_file_validates_like_uploaded_file(bad):
    fields = dict(kind=FileKind.COUNTERPARTIES, checksum="a" * 64, size_bytes=1, stored_path="x")
    fields.update(bad)
    with pytest.raises(ValueError):
        NewFile(**fields)


# --- AnalysisQueue: the worker side of the same storage ---


async def queued_run(repository, owner: int = OWNER):
    run = await repository.create_run(owner, DAY, DataMode.DEMO)
    return await repository.transition(owner, run.id, RunStatus.QUEUED)


async def test_claim_next_takes_the_oldest_queued_run_and_marks_it_running(repository):
    first = await queued_run(repository)
    second = await queued_run(repository)
    claimed = await repository.claim_next()
    assert claimed.id == first.id
    assert claimed.status == RunStatus.RUNNING
    assert claimed.attempts == 1
    assert (await repository.get_run(OWNER, first.id)).status == RunStatus.RUNNING
    assert (await repository.get_run(OWNER, second.id)).status == RunStatus.QUEUED


async def test_claim_next_returns_none_when_nothing_is_queued(repository):
    assert await repository.claim_next() is None
    await repository.create_run(OWNER, DAY, DataMode.DEMO)  # a draft is not queued
    assert await repository.claim_next() is None


async def test_a_run_is_never_claimed_twice(repository):
    await queued_run(repository)
    first = await repository.claim_next()
    assert first is not None
    assert await repository.claim_next() is None


async def test_finish_records_the_outcome_and_safe_failure(repository):
    run = await queued_run(repository)
    await repository.claim_next()
    done = await repository.finish(run.id, RunOutcome(RunStatus.FAILED, "Источник недоступен"))
    assert done.status == RunStatus.FAILED
    assert done.failure == "Источник недоступен"
    reloaded = await repository.get_run(OWNER, run.id)
    assert reloaded == done
    assert await repository.claim_next() is None


async def test_finish_requires_a_running_run(repository):
    run = await queued_run(repository)
    with pytest.raises(InvalidTransition):
        await repository.finish(run.id, RunOutcome(RunStatus.COMPLETED))
    with pytest.raises(RunNotFound):
        await repository.finish("missing", RunOutcome(RunStatus.COMPLETED))


@pytest.mark.parametrize("status", [RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING])
def test_outcome_must_be_a_final_status(status):
    with pytest.raises(ValueError):
        RunOutcome(status)


def test_outcome_failure_text_needs_failed_or_partial():
    with pytest.raises(ValueError):
        RunOutcome(RunStatus.COMPLETED, "x")
    assert RunOutcome(RunStatus.PARTIAL, "часть разделов недоступна").failure


async def test_recover_interrupted_requeues_running_runs_keeping_attempts(repository):
    run = await queued_run(repository)
    await repository.claim_next()
    recovered = await repository.recover_interrupted()
    assert [item.id for item in recovered] == [run.id]
    assert recovered[0].status == RunStatus.QUEUED
    assert recovered[0].attempts == 1
    assert (await repository.claim_next()).attempts == 2


async def test_recover_interrupted_gives_up_after_max_attempts(repository):
    run = await queued_run(repository)
    for _ in range(MAX_ATTEMPTS):
        assert (await repository.claim_next()).id == run.id
        if _ < MAX_ATTEMPTS - 1:
            await repository.recover_interrupted()
    recovered = await repository.recover_interrupted()
    assert recovered[0].status == RunStatus.FAILED
    assert recovered[0].failure
    assert await repository.claim_next() is None


async def test_recover_interrupted_leaves_other_statuses_alone(repository):
    draft = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    queued = await queued_run(repository)
    assert await repository.recover_interrupted() == ()
    assert (await repository.get_run(OWNER, draft.id)).status == RunStatus.DRAFT
    assert (await repository.get_run(OWNER, queued.id)).status == RunStatus.QUEUED


async def test_claim_and_recovery_order_is_creation_order_when_timestamps_tie(tmp_path):
    """Both implementations must break ties the same way: by creation sequence."""
    frozen = lambda: START  # noqa: E731 — a clock that never moves
    for repository in (
        InMemoryAnalysisRepository(clock=frozen),
        open_sqlite_repository(tmp_path / "tie.sqlite3", clock=frozen),
    ):
        runs = [await queued_run(repository) for _ in range(3)]
        assert [run.updated_at for run in runs] == [START] * 3
        assert (await repository.claim_next()).id == runs[0].id
        assert (await repository.claim_next()).id == runs[1].id
        assert (await repository.claim_next()).id == runs[2].id
        recovered = await repository.recover_interrupted()
        assert [run.id for run in recovered] == [run.id for run in runs]
