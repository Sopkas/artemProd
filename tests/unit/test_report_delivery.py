"""fetch_report / confirm_delivery: read the latest report, never touch the queue."""

from datetime import date

import pytest

from claims_assistant.application.report_delivery import (
    ReportUnavailable,
    confirm_delivery,
    fetch_report,
)
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.steps import DeliveryStatus
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

OWNER = 42
DAY = date(2026, 9, 19)


@pytest.fixture
def storage(tmp_path):
    return LocalFileStorage(tmp_path / "uploads")


async def test_no_runs_or_no_report_gives_none(storage):
    repository = InMemoryAnalysisRepository()
    assert await fetch_report(OWNER, repository, storage) is None
    await repository.create_run(OWNER, DAY, DataMode.DEMO)
    assert await fetch_report(OWNER, repository, storage) is None


async def test_latest_report_is_read_and_delivery_confirmed(storage):
    repository = InMemoryAnalysisRepository()
    older = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.save_report(older.id, storage.save(older.id, b"old"))
    newer = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.save_report(newer.id, storage.save(newer.id, b"new"))
    report = await fetch_report(OWNER, repository, storage)
    assert report.run.id == newer.id and report.data == b"new"
    assert report.filename == "otchet-2026-09-19.xlsx"
    assert report.artifact.delivery is DeliveryStatus.PENDING
    confirmed = await confirm_delivery(report, repository)
    assert confirmed.delivery is DeliveryStatus.DELIVERED
    assert (await repository.get_run(OWNER, newer.id)).status == RunStatus.DRAFT
    assert await repository.claim_next() is None


async def test_missing_file_marks_failed_and_raises(storage):
    repository = InMemoryAnalysisRepository()
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    path = storage.save(run.id, b"x")
    await repository.save_report(run.id, path)
    storage.remove(path)
    with pytest.raises(ReportUnavailable):
        await fetch_report(OWNER, repository, storage)
    assert (await repository.get_report(OWNER, run.id)).delivery is DeliveryStatus.FAILED


async def test_other_owner_sees_nothing(storage):
    repository = InMemoryAnalysisRepository()
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.save_report(run.id, storage.save(run.id, b"x"))
    assert await fetch_report(99, repository, storage) is None
