"""S6-02: expired checks are purged with their files; live ones are never touched."""

from datetime import UTC, date, datetime, timedelta

import pytest

from claims_assistant.application.analysis_queue import RunOutcome
from claims_assistant.application.check_package import StorageError, accept_counterparties
from claims_assistant.application.retention import (
    PurgeSummary,
    RetentionSweeper,
    purge_expired,
)
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

OWNER = 42
DAY = date(2026, 9, 1)
RETENTION = timedelta(days=30)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
def storage(request, tmp_path):
    clock = Clock(datetime(2026, 8, 1, 12, 0, tzinfo=UTC))
    if request.param == "memory":
        repository = InMemoryAnalysisRepository(clock=clock)
    else:
        repository = open_sqlite_repository(tmp_path / "claims.sqlite3", clock=clock)
    files = LocalFileStorage(tmp_path / "uploads")
    yield repository, files, clock
    if request.param == "sqlite":
        repository.close()


async def make_run(repository, files, *, status: RunStatus, with_report: bool = False):
    result = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(),
        repository=repository,
        files=files,
        reader=OpenpyxlSheetReader(),
    )
    run = result.run
    if status is not RunStatus.DRAFT:
        await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    if status in (RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED):
        await repository.claim_next()
    if status in (RunStatus.COMPLETED, RunStatus.FAILED):
        outcome = (
            RunOutcome(RunStatus.COMPLETED)
            if status is RunStatus.COMPLETED
            else RunOutcome(RunStatus.FAILED, "Ошибка.")
        )
        await repository.finish(run.id, outcome)
    if with_report:
        await repository.save_report(run.id, files.save(run.id, b"report"))
    return await repository.get_run(OWNER, run.id) if status is not RunStatus.RUNNING else run


async def test_old_finished_runs_go_with_their_files_and_records(storage, tmp_path):
    repository, files, clock = storage
    old = await make_run(repository, files, status=RunStatus.COMPLETED, with_report=True)
    old_draft = await make_run(repository, files, status=RunStatus.DRAFT)
    clock.now += timedelta(days=31)
    fresh = await make_run(repository, files, status=RunStatus.COMPLETED)
    run_dirs = {p.name for p in (tmp_path / "uploads").iterdir()}
    assert {old.id, old_draft.id, fresh.id} <= run_dirs

    summary = await purge_expired(repository, files, retention=RETENTION, now=clock.now)
    assert summary == PurgeSummary(expired=2, removed=2, failed=0)
    remaining = [run.id for run in await repository.list_runs(OWNER)]
    assert remaining == [fresh.id]
    assert {p.name for p in (tmp_path / "uploads").iterdir()} == {fresh.id}
    assert await repository.get_report(OWNER, fresh.id) is None
    assert await repository.list_steps(old.id) == ()
    # A second sweep finds nothing and changes nothing.
    assert await purge_expired(repository, files, retention=RETENTION, now=clock.now) == (
        PurgeSummary(expired=0, removed=0, failed=0)
    )


async def test_queued_and_running_runs_are_never_purged(storage):
    repository, files, clock = storage
    queued = await make_run(repository, files, status=RunStatus.QUEUED)
    running = await make_run(repository, files, status=RunStatus.RUNNING)
    clock.now += timedelta(days=400)
    summary = await purge_expired(repository, files, retention=RETENTION, now=clock.now)
    assert summary.expired == 0
    ids = {run.id for run in await repository.list_runs(OWNER)}
    assert ids == {queued.id, running.id}


async def test_dry_run_counts_but_keeps_everything(storage, tmp_path):
    repository, files, clock = storage
    old = await make_run(repository, files, status=RunStatus.FAILED)
    clock.now += timedelta(days=31)
    summary = await purge_expired(
        repository, files, retention=RETENTION, now=clock.now, dry_run=True
    )
    assert summary == PurgeSummary(expired=1, removed=0, failed=0, dry_run=True)
    assert (await repository.get_run(OWNER, old.id)).id == old.id
    assert (tmp_path / "uploads" / old.id).exists()


async def test_files_that_cannot_be_removed_keep_the_record_for_a_retry(storage, monkeypatch):
    repository, files, clock = storage
    old = await make_run(repository, files, status=RunStatus.COMPLETED)
    clock.now += timedelta(days=31)

    def refuse(run_id):
        raise StorageError("locked")

    monkeypatch.setattr(files, "remove_run", refuse)
    summary = await purge_expired(repository, files, retention=RETENTION, now=clock.now)
    assert summary == PurgeSummary(expired=1, removed=0, failed=1)
    assert (await repository.get_run(OWNER, old.id)).id == old.id


async def test_retention_must_be_positive(storage):
    repository, files, clock = storage
    with pytest.raises(ValueError):
        await purge_expired(repository, files, retention=timedelta(0), now=clock.now)


async def eventually(check, timeout: float = 2.0) -> None:
    """The sweeper works in its own task: wait for what it does, not for a fixed pause.

    A pause of 0.05 s was outlasted by the Windows runner on main (30.09.2026): the purge
    had not finished yet, and the test failed with nothing wrong in the sweeper.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await check():
        if loop.time() > deadline:
            raise AssertionError("the sweeper did not get there in time")
        await asyncio.sleep(0.01)


async def test_sweeper_purges_at_start_and_stops_promptly(storage):
    import asyncio

    repository, files, clock = storage
    await make_run(repository, files, status=RunStatus.COMPLETED)
    clock.now += timedelta(days=31)
    sweeper = RetentionSweeper(
        repository, files, retention=RETENTION, interval=3600, clock=lambda: clock.now
    )
    task = asyncio.create_task(sweeper.run_forever())

    async def purged():
        return await repository.list_runs(OWNER) == ()

    await eventually(purged)
    sweeper.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_sweeper_survives_a_storage_failure(storage, caplog):
    import asyncio
    import logging

    repository, files, clock = storage

    class Broken:
        async def list_expired(self, before):
            raise RuntimeError("db gone with secret path")

        async def delete_run(self, run_id):
            raise AssertionError

    sweeper = RetentionSweeper(Broken(), files, retention=RETENTION, interval=3600)
    task = asyncio.create_task(sweeper.run_forever())
    with caplog.at_level(logging.ERROR):

        async def logged():
            return "retention_sweep_failed" in caplog.text

        await eventually(logged)
    sweeper.stop()
    await asyncio.wait_for(task, timeout=1)
    assert "retention_sweep_failed" in caplog.text and "secret path" not in caplog.text


async def test_run_relaunched_after_the_listing_is_not_purged(storage):
    """Review B on #33: the delete carries the same condition as the listing."""
    repository, files, clock = storage
    old = await make_run(repository, files, status=RunStatus.DRAFT)
    clock.now += timedelta(days=31)
    before = clock.now - RETENTION

    class Racing:
        """Lists the draft as expired, then the user launches it before the sweep acts."""

        def __init__(self, inner):
            self.inner = inner

        async def list_expired(self, before):
            expired = await self.inner.list_expired(before)
            await self.inner.transition(OWNER, old.id, RunStatus.QUEUED)
            return expired

        def __getattr__(self, name):
            return getattr(self.inner, name)

    summary = await purge_expired(Racing(repository), files, retention=RETENTION, now=clock.now)
    assert summary == PurgeSummary(expired=1, removed=0, failed=0)
    run = await repository.get_run(OWNER, old.id)
    assert run.status == RunStatus.QUEUED and len(run.files) == 1
    assert files.read(run.files[0].stored_path)
    assert await repository.delete_run(old.id, before) is False
