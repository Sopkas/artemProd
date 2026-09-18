"""Sprint-2 processor: re-read the stored package and record how usable it is."""

from datetime import date
from decimal import Decimal

from claims_assistant.application.check_package import accept_counterparties
from claims_assistant.application.package_processor import PackageProcessor
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

OWNER = 42
DAY = date(2026, 9, 1)


async def accepted(tmp_path, data: bytes):
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    result = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, data, repository=repository, files=files, reader=OpenpyxlSheetReader()
    )
    await repository.transition(OWNER, result.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    return run, files


async def test_clean_package_completes(tmp_path):
    run, files = await accepted(tmp_path, build_counterparties_template())
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.COMPLETED
    assert outcome.failure is None


async def test_package_with_row_errors_is_partial_with_a_count_not_contents(tmp_path):
    rows = (
        CounterpartyRow(inn="1234567894"),
        CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=DAY),
    )
    run, files = await accepted(tmp_path, build_counterparties_template(rows))
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.PARTIAL
    assert "1" in outcome.failure
    assert "1234567894" not in outcome.failure


async def test_missing_file_on_disk_fails_the_run(tmp_path):
    run, files = await accepted(tmp_path, build_counterparties_template())
    for stored in run.files:
        files.remove(stored.stored_path)
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.FAILED
    assert outcome.failure


async def test_run_without_counterparties_file_fails(tmp_path):
    repository = InMemoryAnalysisRepository()
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    files = LocalFileStorage(tmp_path / "uploads")
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.FAILED
