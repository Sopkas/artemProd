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
        OWNER,
        DAY,
        DataMode.DEMO,
        data,
        repository=repository,
        files=files,
        reader=OpenpyxlSheetReader(),
    )
    await repository.transition(OWNER, result.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    return run, files, repository


async def test_clean_package_completes(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template())
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.COMPLETED
    assert outcome.failure is None


async def test_package_with_row_errors_is_partial_with_a_count_not_contents(tmp_path):
    rows = (
        CounterpartyRow(inn="1234567894"),
        CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=DAY),
    )
    run, files, repository = await accepted(tmp_path, build_counterparties_template(rows))
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.PARTIAL
    assert "1" in outcome.failure
    assert "1234567894" not in outcome.failure


async def test_missing_file_on_disk_fails_the_run(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template())
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


async def test_file_with_no_usable_rows_fails_with_its_own_reason(tmp_path):
    from claims_assistant.application.package_processor import NO_USABLE_ROWS
    from claims_assistant.domain.counterparties import CounterpartyRow

    # Accepted with the matching cut-off date, then re-processed under a different one:
    # every row now fails the cut-off rule, so nothing usable remains.
    rows = (CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=DAY),)
    run, files, repository = await accepted(tmp_path, build_counterparties_template(rows))
    from dataclasses import replace

    shifted = replace(run, analysis_date=date(2026, 9, 2))
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(shifted)
    assert outcome.status == RunStatus.FAILED
    assert outcome.failure == NO_USABLE_ROWS


# --- S3-03: saved steps are not executed again on resume ---


class CountingReader:
    def __init__(self, inner):
        self.inner = inner
        self.reads = 0

    def read(self, source, sheet, limits):
        self.reads += 1
        return self.inner.read(source, sheet, limits)


async def test_import_step_is_saved_and_skipped_after_an_interruption(tmp_path):
    from claims_assistant.application.package_processor import IMPORT_STEP, IMPORT_VERSION
    from claims_assistant.domain.steps import RUN_SCOPE, StepStatus

    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    reader = CountingReader(OpenpyxlSheetReader())
    accepted_run = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(),
        repository=repository,
        files=files,
        reader=reader,
    )
    await repository.transition(OWNER, accepted_run.run.id, RunStatus.QUEUED)
    reads_before = reader.reads
    processor = PackageProcessor(files, reader, steps=repository)

    run = await repository.claim_next()
    first = await processor.process(run)
    assert first.status == RunStatus.COMPLETED
    assert reader.reads == reads_before + 1
    saved = await repository.get_step(run.id, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION)
    assert saved is not None and saved.status is StepStatus.OK
    assert '"rows": 2' in saved.payload

    # The process died before finish(); on restart the run is requeued and claimed again.
    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    assert resumed.id == run.id and resumed.attempts == 2
    second = await processor.process(resumed)
    assert second == first
    assert reader.reads == reads_before + 1  # the import was not run a second time


async def test_failed_import_is_recorded_as_a_failed_step(tmp_path):
    from claims_assistant.application.package_processor import IMPORT_STEP, IMPORT_VERSION
    from claims_assistant.domain.steps import RUN_SCOPE, StepStatus

    run, files, repository = await accepted(tmp_path, build_counterparties_template())
    for stored in run.files:
        files.remove(stored.stored_path)
    outcome = await PackageProcessor(files, OpenpyxlSheetReader(), steps=repository).process(run)
    assert outcome.status == RunStatus.FAILED
    step = await repository.get_step(run.id, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION)
    assert step is not None and step.status is StepStatus.FAILED and step.error


async def test_processor_without_a_step_store_still_works(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template())
    outcome = await PackageProcessor(files, OpenpyxlSheetReader()).process(run)
    assert outcome.status == RunStatus.COMPLETED
