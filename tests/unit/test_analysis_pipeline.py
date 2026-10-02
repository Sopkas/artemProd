"""S3-01: import → external data → rules → report, with saved steps (S3-03)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook

from claims_assistant.application.analysis_pipeline import (
    FETCH_STEP,
    FETCH_VERSION,
    IMPORT_STEP,
    IMPORT_VERSION,
    MODE_MISMATCH,
    NO_USABLE_ROWS,
    REPORT_STEP,
    REPORT_VERSION,
    REPORT_WRITE_FAILED,
    STEP_UNREADABLE,
    AnalysisPipeline,
    RunLimits,
    RunSummary,
    load_summary,
)
from claims_assistant.application.check_package import StorageError, accept_counterparties
from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.application.external_guard import GuardedCompanyDataProvider, GuardPolicy
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.scoring import Priority
from claims_assistant.domain.steps import RUN_SCOPE, DeliveryStatus, StepStatus
from claims_assistant.infrastructure.cache.memory import TtlSnapshotCache
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.excel.report import build_report
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

OWNER = 42
DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ROWS = (
    CounterpartyRow(inn="1234567894", name="ООО Ромашка", debt=Decimal("100.00"), cutoff_date=DAY),
    CounterpartyRow(inn="7707083893", debt=Decimal("5.00"), cutoff_date=DAY, overdue_days=90),
)


class CountingProvider:
    """Wraps a provider and counts calls; can be told to fail sections like a real source."""

    def __init__(self, inner, failing: set[str] | None = None) -> None:
        self.inner = inner
        self.failing = failing or set()
        self.calls: list[str] = []

    async def fetch(self, request: CompanyDataRequest):
        self.calls.append(request.inn)
        if request.inn in self.failing:
            return tuple(
                ExternalSnapshot(
                    inn=request.inn,
                    section=section,
                    source="checko",
                    mode=DataMode.DEMO,
                    fetched_at=NOW,
                    status=FetchStatus.UNAVAILABLE,
                    coverage=Coverage.UNAVAILABLE,
                    missing=("Источник недоступен.",),
                    error=ProviderError("http_error", "Источник недоступен."),
                )
                for section in request.sections
            )
        return await self.inner.fetch(request)


class CountingReader:
    def __init__(self, inner):
        self.inner = inner
        self.reads = 0

    def read(self, source, sheet, limits):
        self.reads += 1
        return self.inner.read(source, sheet, limits)


def guard(inner, *, clock=lambda: NOW, retries: int = 0):
    async def no_sleep(_seconds: float) -> None:
        return None

    return GuardedCompanyDataProvider(
        inner,
        GuardPolicy(max_retries=retries),
        TtlSnapshotCache(3600, clock=clock),
        mode=DataMode.DEMO,
        clock=clock,
        sleep=no_sleep,
    )


async def accepted(tmp_path, data: bytes, *, reader=None, mode=DataMode.DEMO):
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    result = await accept_counterparties(
        OWNER,
        DAY,
        mode,
        data,
        repository=repository,
        files=files,
        reader=reader or OpenpyxlSheetReader(),
    )
    await repository.transition(OWNER, result.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    return run, files, repository


def pipeline(files, repository, provider, *, reader=None, mode=DataMode.DEMO, **options):
    return AnalysisPipeline(
        files,
        reader or OpenpyxlSheetReader(),
        provider,
        repository,
        mode=mode,
        build_report=build_report,
        clock=options.pop("clock", lambda: NOW),
        **options,
    )


def sheet_rows(data: bytes, sheet: str) -> list[tuple]:
    workbook = load_workbook(BytesIO(data), read_only=True)
    return [tuple(row) for row in workbook[sheet].iter_rows(min_row=3, values_only=True)]


# --- the whole scenario --------------------------------------------------------------


async def test_clean_package_yields_a_report_with_real_priorities(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = CountingProvider(DemoCompanyDataProvider(DemoScenario.ALARM))
    outcome = await pipeline(files, repository, guard(provider)).process(run)

    assert outcome.status == RunStatus.COMPLETED and outcome.failure is None
    assert provider.calls == ["1234567894", "7707083893"]
    artifact = await repository.get_report(OWNER, run.id)
    assert artifact is not None and artifact.delivery is DeliveryStatus.PENDING
    data = files.read(artifact.stored_path)
    priorities = sheet_rows(data, "Приоритеты")
    assert {row[0] for row in priorities} == {"1234567894", "7707083893"}
    # The alarm scenario carries an unresolved bankruptcy event: the rules fire
    # (high, not the predefined demo scores of the control package).
    assert all(row[4] == "Высокий" for row in priorities)
    assert not any("Демонстрационная оценка" in str(cell) for row in priorities for cell in row)
    summary = await load_summary(run.id, repository)
    assert summary == RunSummary(
        companies=2,
        checked=2,
        unchecked=0,
        row_errors=0,
        priorities={**{p: 0 for p in Priority}, Priority.HIGH: 2},
        budget_exhausted=False,
    )


async def test_report_meta_names_the_run_mode_and_the_check_time(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    artifact = await repository.get_report(OWNER, run.id)
    about = sheet_rows(files.read(artifact.stored_path), "О проверке")
    text = " ".join(str(cell) for row in about for cell in row if cell is not None)
    assert run.id in text and "демо" in text.lower()
    assert "Контрагенты: 2 строк" in text


async def test_row_errors_make_the_run_partial_without_leaking_contents(tmp_path):
    rows = (
        CounterpartyRow(inn="1234567894"),
        CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=DAY),
    )
    run, files, repository = await accepted(tmp_path, build_counterparties_template(rows))
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert outcome.status == RunStatus.PARTIAL
    assert "Строк с ошибками: 1" in outcome.failure
    assert "1234567894" not in outcome.failure and "5.00" not in outcome.failure
    # The usable row is still reported.
    artifact = await repository.get_report(OWNER, run.id)
    assert artifact is not None


async def test_source_failure_stays_in_the_report_and_is_never_replaced_by_demo(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = CountingProvider(DemoCompanyDataProvider(), failing={"7707083893"})
    outcome = await pipeline(files, repository, guard(provider)).process(run)

    assert outcome.status == RunStatus.PARTIAL
    assert "Организаций с неполными внешними данными: 1" in outcome.failure
    artifact = await repository.get_report(OWNER, run.id)
    priorities = {row[0]: row for row in sheet_rows(files.read(artifact.stored_path), "Приоритеты")}
    assert "недоступны" in str(priorities["7707083893"][5])
    assert "недоступны" not in str(priorities["1234567894"][5])
    summary = await load_summary(run.id, repository)
    assert (summary.checked, summary.unchecked, summary.budget_exhausted) == (1, 1, False)


async def test_exhausted_budget_keeps_the_checked_part_and_reports_partial(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = CountingProvider(DemoCompanyDataProvider())
    # Three sections per company: the second company runs out of requests.
    limits = RunLimits(max_requests=3, max_seconds=900)
    outcome = await pipeline(files, repository, guard(provider), limits=limits).process(run)

    assert outcome.status == RunStatus.PARTIAL
    assert "Лимит времени или запросов" in outcome.failure
    assert provider.calls == ["1234567894"]
    summary = await load_summary(run.id, repository)
    assert (summary.checked, summary.unchecked, summary.budget_exhausted) == (1, 1, True)
    # The unchecked company has no saved fetch step: a new attempt would query it.
    assert await repository.get_step(run.id, "7707083893", FETCH_STEP, FETCH_VERSION) is None
    assert await repository.get_step(run.id, "1234567894", FETCH_STEP, FETCH_VERSION) is not None


async def test_run_time_limit_is_measured_from_the_pipeline_clock(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    ticks = iter([NOW + timedelta(seconds=n * 100) for n in range(20)])
    clock = lambda: next(ticks)  # noqa: E731
    provider = CountingProvider(DemoCompanyDataProvider())
    limits = RunLimits(max_requests=100, max_seconds=150)
    outcome = await pipeline(
        files, repository, guard(provider, clock=clock), limits=limits, clock=clock
    ).process(run)
    assert outcome.status == RunStatus.PARTIAL and "Лимит" in outcome.failure
    assert provider.calls == ["1234567894"]


# --- explicit mode ----------------------------------------------------------------


async def test_run_of_another_mode_fails_instead_of_using_the_wrong_provider(tmp_path):
    run, files, repository = await accepted(
        tmp_path, build_counterparties_template(ROWS), mode=DataMode.LIVE
    )
    provider = CountingProvider(DemoCompanyDataProvider())
    outcome = await pipeline(files, repository, guard(provider), mode=DataMode.DEMO).process(run)
    assert outcome == outcome.__class__(RunStatus.FAILED, MODE_MISMATCH)
    assert provider.calls == []
    assert await repository.get_report(OWNER, run.id) is None


# --- package problems -------------------------------------------------------------


async def test_missing_file_on_disk_fails_the_run_and_records_the_step(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template())
    for stored in run.files:
        files.remove(stored.stored_path)
    provider = CountingProvider(DemoCompanyDataProvider())
    outcome = await pipeline(files, repository, guard(provider)).process(run)
    assert outcome.status == RunStatus.FAILED and outcome.failure
    assert provider.calls == []
    step = await repository.get_step(run.id, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION)
    assert step is not None and step.status is StepStatus.FAILED and step.error


async def test_run_without_counterparties_file_fails(tmp_path):
    repository = InMemoryAnalysisRepository()
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    files = LocalFileStorage(tmp_path / "uploads")
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert outcome.status == RunStatus.FAILED


async def test_file_with_no_usable_rows_fails_with_its_own_reason(tmp_path):
    rows = (CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=DAY),)
    run, files, repository = await accepted(tmp_path, build_counterparties_template(rows))
    shifted = replace(run, analysis_date=date(2026, 9, 2))
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(shifted)
    assert outcome.status == RunStatus.FAILED
    assert outcome.failure == NO_USABLE_ROWS


async def test_report_write_failure_is_a_failed_run_not_a_completed_one(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))

    class BrokenStorage:
        def read(self, stored_path):
            return files.read(stored_path)

        def save(self, run_id, data):
            raise StorageError("disk full")

        def remove(self, stored_path):
            files.remove(stored_path)

    outcome = await pipeline(BrokenStorage(), repository, guard(DemoCompanyDataProvider())).process(
        run
    )
    assert outcome.status == RunStatus.FAILED and outcome.failure == REPORT_WRITE_FAILED
    assert await repository.get_report(OWNER, run.id) is None


# --- S3-03: saved steps are not executed again on resume ---------------------------


async def test_interrupted_run_resumes_without_repeating_import_or_requests(tmp_path):
    reader = CountingReader(OpenpyxlSheetReader())
    run, files, repository = await accepted(
        tmp_path, build_counterparties_template(ROWS), reader=reader
    )
    provider = CountingProvider(DemoCompanyDataProvider())
    reads_before = reader.reads

    class DiesAfterFetch(Exception):
        pass

    class Storage:
        """Dies on the first report write: the process crashed after the fetches."""

        def __init__(self) -> None:
            self.attempts = 0

        def read(self, stored_path):
            return files.read(stored_path)

        def save(self, run_id, data):
            self.attempts += 1
            if self.attempts == 1:
                raise DiesAfterFetch()
            return files.save(run_id, data)

        def remove(self, stored_path):
            files.remove(stored_path)

    storage = Storage()
    with pytest.raises(DiesAfterFetch):
        await pipeline(storage, repository, guard(provider), reader=reader).process(run)
    assert reader.reads == reads_before + 1
    assert provider.calls == ["1234567894", "7707083893"]

    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    assert resumed.id == run.id and resumed.attempts == 2
    outcome = await pipeline(storage, repository, guard(provider), reader=reader).process(resumed)
    assert outcome.status == RunStatus.COMPLETED
    assert reader.reads == reads_before + 1  # import not repeated
    assert provider.calls == ["1234567894", "7707083893"]  # no second external request
    assert await repository.get_report(OWNER, run.id) is not None


async def test_finished_pipeline_is_not_rebuilt_on_a_repeated_claim(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = CountingProvider(DemoCompanyDataProvider())
    first = await pipeline(files, repository, guard(provider)).process(run)
    artifact = await repository.get_report(OWNER, run.id)

    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    second = await pipeline(files, repository, guard(provider)).process(resumed)
    assert second == first
    assert provider.calls == ["1234567894", "7707083893"]
    assert (await repository.get_report(OWNER, run.id)) == artifact
    step = await repository.get_step(run.id, RUN_SCOPE, REPORT_STEP, REPORT_VERSION)
    assert step is not None and step.status is StepStatus.OK


async def test_saved_fetch_step_is_read_back_into_the_same_snapshots(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    await pipeline(
        files, repository, guard(DemoCompanyDataProvider(DemoScenario.INCOMPLETE))
    ).process(run)
    from claims_assistant.application.step_payloads import load_snapshots

    step = await repository.get_step(run.id, "1234567894", FETCH_STEP, FETCH_VERSION)
    restored = load_snapshots(step.payload)
    expected = await DemoCompanyDataProvider(DemoScenario.INCOMPLETE).fetch(
        CompanyDataRequest(inn="1234567894", sections=tuple(Section))
    )
    assert restored == expected


async def test_unreadable_saved_step_fails_the_run_safely(tmp_path):
    from claims_assistant.domain.steps import StepResult

    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    await repository.save_step(
        StepResult(
            run_id=run.id,
            inn=RUN_SCOPE,
            step=IMPORT_STEP,
            version=IMPORT_VERSION,
            status=StepStatus.OK,
            completed_at=NOW,
            payload="{not json",
        )
    )
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert outcome.status == RunStatus.FAILED and outcome.failure == STEP_UNREADABLE


async def test_pipeline_resumes_from_sqlite_after_a_process_restart(tmp_path):
    """Steps and the report survive a real reopen of the database (S3-03 on SQLite)."""
    from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository

    path = tmp_path / "claims.sqlite3"
    files = LocalFileStorage(tmp_path / "uploads")
    reader = CountingReader(OpenpyxlSheetReader())
    provider = CountingProvider(DemoCompanyDataProvider())
    first = open_sqlite_repository(path)
    try:
        result = await accept_counterparties(
            OWNER,
            DAY,
            DataMode.DEMO,
            build_counterparties_template(ROWS),
            repository=first,
            files=files,
            reader=reader,
        )
        await first.transition(OWNER, result.run.id, RunStatus.QUEUED)
        run = await first.claim_next()
        reads_before = reader.reads
        # Fetch every company, then "die" before the report is built.
        await pipeline(files, first, guard(provider), reader=reader)._import(run)
        await pipeline(files, first, guard(provider), reader=reader)._fetch_all(run, ROWS)
    finally:
        first.close()

    second = open_sqlite_repository(path)
    try:
        await second.recover_interrupted()
        resumed = await second.claim_next()
        outcome = await pipeline(files, second, guard(provider), reader=reader).process(resumed)
        assert outcome.status == RunStatus.COMPLETED
        assert reader.reads == reads_before + 1
        assert provider.calls == ["1234567894", "7707083893"]
        assert await second.get_report(OWNER, run.id) is not None
    finally:
        second.close()


async def test_transient_source_failure_is_not_saved_so_a_resumed_run_retries_it(tmp_path):
    """Review B on #25: after a crash the temporary failure is often gone."""
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = CountingProvider(DemoCompanyDataProvider(), failing={"7707083893"})
    # The process died right after the fetches, before the report.
    await pipeline(files, repository, guard(provider))._fetch_all(run, ROWS)
    assert provider.calls == ["1234567894", "7707083893"]
    # http_error is transient: after the guard's retries the step stays open.
    assert await repository.get_step(run.id, "7707083893", FETCH_STEP, FETCH_VERSION) is None
    assert await repository.get_step(run.id, "1234567894", FETCH_STEP, FETCH_VERSION) is not None

    provider.failing.clear()
    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    outcome = await pipeline(files, repository, guard(provider)).process(resumed)
    assert outcome.status == RunStatus.COMPLETED  # the source answered this time
    assert provider.calls == ["1234567894", "7707083893", "7707083893"]


async def test_final_source_answers_are_saved(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))

    class NotFound:
        async def fetch(self, request):
            return tuple(
                ExternalSnapshot(
                    inn=request.inn,
                    section=section,
                    source="checko",
                    mode=DataMode.DEMO,
                    fetched_at=NOW,
                    status=FetchStatus.NOT_FOUND,
                    coverage=Coverage.UNAVAILABLE,
                    missing=("Организация не найдена.",),
                    error=ProviderError("not_found", "Организация не найдена."),
                )
                for section in request.sections
            )

    outcome = await pipeline(files, repository, guard(NotFound())).process(run)
    assert outcome.status == RunStatus.PARTIAL
    for row in ROWS:
        assert await repository.get_step(run.id, row.inn, FETCH_STEP, FETCH_VERSION) is not None


async def test_report_built_before_a_crash_is_replaced_not_orphaned(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = guard(DemoCompanyDataProvider())
    # The process died between save_report and the report step.
    stale = files.save(run.id, b"stale-report")
    await repository.save_report(run.id, stale)
    outcome = await pipeline(files, repository, provider).process(run)
    assert outcome.status == RunStatus.COMPLETED
    artifact = await repository.get_report(OWNER, run.id)
    assert artifact.stored_path != stale
    with pytest.raises(StorageError):
        files.read(stale)


async def test_package_line_counts_rows_with_errors_and_usable_rows(tmp_path):
    rows = (
        CounterpartyRow(inn="1234567894"),
        CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=DAY),
        CounterpartyRow(inn="7707083893", debt=Decimal("1.00"), cutoff_date=DAY),
    )
    run, files, repository = await accepted(tmp_path, build_counterparties_template(rows))
    await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    artifact = await repository.get_report(OWNER, run.id)
    about = sheet_rows(files.read(artifact.stored_path), "О проверке")
    text = " ".join(str(cell) for row in about for cell in row if cell is not None)
    assert "Контрагенты: 3 строк, пригодных 2" in text


async def test_report_names_the_optional_files_of_the_package(tmp_path):
    from claims_assistant.application.check_package import accept_ledger
    from claims_assistant.domain.analysis import FileKind
    from claims_assistant.domain.external import Period
    from claims_assistant.infrastructure.excel.ledgers import build_payments_workbook

    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    reader = OpenpyxlSheetReader()
    accepted_run = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(ROWS),
        repository=repository,
        files=files,
        reader=reader,
    )
    # Two exports: each line counts its own file, not every payment of the package
    # (block 5 of the manual test, 30.09.2026: both lines said the total).
    for rows, start in (
        ([["1234567894", "P-1", date(2026, 7, 15), 100.0]], date(2026, 6, 1)),
        (
            [
                ["7707083893", "P-2", date(2026, 7, 20), 5.0],
                ["7707083893", "P-3", date(2026, 8, 20), 5.0],
            ],
            date(2026, 7, 1),
        ),
    ):
        await accept_ledger(
            OWNER,
            accepted_run.run.id,
            FileKind.PAYMENTS,
            build_payments_workbook(rows),
            coverage=Period(start, date(2026, 8, 31)),
            repository=repository,
            files=files,
            reader=reader,
        )
    await repository.transition(OWNER, accepted_run.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    artifact = await repository.get_report(OWNER, run.id)
    about = sheet_rows(files.read(artifact.stored_path), "О проверке")
    text = " ".join(str(cell) for row in about for cell in row if cell is not None)
    assert "Контрагенты: 2 строк, пригодных 2" in text
    assert (
        "Платежи за 01.06.2026–31.08.2026, файл 1: использован — давность платежа; "
        "пригодных строк: 1"
    ) in text
    assert (
        "Платежи за 01.07.2026–31.08.2026, файл 2: использован — давность платежа; "
        "пригодных строк: 2"
    ) in text


async def test_internal_indicators_and_chronology_reach_the_report(tmp_path):
    """S4-04: the owner's own files drive the assessment, the report names the files
    used, lists what is missing and shows the interactions chronology."""
    from claims_assistant.application.check_package import accept_ledger
    from claims_assistant.domain.analysis import FileKind
    from claims_assistant.domain.external import Period
    from claims_assistant.infrastructure.excel.ledgers import (
        build_debt_history_workbook,
        build_interactions_workbook,
        build_payments_workbook,
    )

    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    reader = OpenpyxlSheetReader()
    rows = (
        CounterpartyRow(
            inn="1234567894",
            name="ООО Ромашка",
            debt=Decimal("200.00"),
            cutoff_date=DAY,
            last_payment_date=date(2026, 7, 15),
        ),
        CounterpartyRow(inn="7707083893", debt=Decimal("5.00"), cutoff_date=DAY),
    )
    accepted_run = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(rows),
        repository=repository,
        files=files,
        reader=reader,
    )
    run_id = accepted_run.run.id
    deps = dict(repository=repository, files=files, reader=reader)
    # A full payments export up to the analysis date confirms the last payment.
    await accept_ledger(
        OWNER,
        run_id,
        FileKind.PAYMENTS,
        build_payments_workbook([["1234567894", "P-1", date(2026, 7, 15), 50.0]]),
        coverage=Period(date(2026, 6, 1), DAY),
        **deps,
    )
    # Debt doubled against the snapshot a month before.
    await accept_ledger(
        OWNER,
        run_id,
        FileKind.DEBT_HISTORY,
        build_debt_history_workbook([["1234567894", date(2026, 8, 1), 100.0]]),
        coverage=None,
        **deps,
    )
    await accept_ledger(
        OWNER,
        run_id,
        FileKind.INTERACTIONS,
        build_interactions_workbook(
            [
                ["1234567894", "I-2", date(2026, 8, 20), "Обещали оплатить.", "телефон"],
                ["1234567894", "I-1", date(2026, 8, 1), "Направлена претензия.", None],
            ]
        ),
        coverage=None,
        **deps,
    )
    await repository.transition(OWNER, run_id, RunStatus.QUEUED)
    run = await repository.claim_next()
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert outcome.status == RunStatus.COMPLETED

    artifact = await repository.get_report(OWNER, run.id)
    data = files.read(artifact.stored_path)
    priorities = {row[0]: row for row in sheet_rows(data, "Приоритеты")}
    assert (
        "долг вырос" in priorities["1234567894"][6].lower() or "×2" in priorities["1234567894"][6]
    )
    about = " ".join(str(c) for row in sheet_rows(data, "О проверке") for c in row if c is not None)
    assert "Платежи за 01.06.2026–01.09.2026: использован — давность платежа" in about
    assert "История долга: использован — динамика долга за месяц" in about
    assert "Взаимодействия: использован — лист «Хронология»" in about
    chronology = [row for row in sheet_rows(data, "Хронология") if row[0]]
    assert [(r[0], r[2], r[3]) for r in chronology] == [
        ("1234567894", "01.08.2026", "I-1"),
        ("1234567894", "20.08.2026", "I-2"),
    ]
    assert chronology[1][5] == "Обещали оплатить."
    quality = " ".join(
        str(c) for row in sheet_rows(data, "Качество данных") for c in row if c is not None
    )
    # The second company has no history: the report says what is missing.
    assert "7707083893" in quality


async def test_resumed_run_builds_the_same_report_without_rereading_optional_files(tmp_path):
    from claims_assistant.application.check_package import accept_ledger
    from claims_assistant.domain.analysis import FileKind
    from claims_assistant.infrastructure.excel.ledgers import build_interactions_workbook

    reader = CountingReader(OpenpyxlSheetReader())
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    accepted_run = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(ROWS),
        repository=repository,
        files=files,
        reader=reader,
    )
    await accept_ledger(
        OWNER,
        accepted_run.run.id,
        FileKind.INTERACTIONS,
        build_interactions_workbook([["1234567894", "I-1", date(2026, 8, 1), "Звонок.", None]]),
        coverage=None,
        repository=repository,
        files=files,
        reader=reader,
    )
    await repository.transition(OWNER, accepted_run.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    provider = CountingProvider(DemoCompanyDataProvider())
    await pipeline(files, repository, guard(provider), reader=reader)._import(run)
    reads_after_import = reader.reads
    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    outcome = await pipeline(files, repository, guard(provider), reader=reader).process(resumed)
    assert outcome.status == RunStatus.COMPLETED
    assert reader.reads == reads_after_import  # neither the main nor the optional files
    artifact = await repository.get_report(OWNER, run.id)
    chronology = [
        row for row in sheet_rows(files.read(artifact.stored_path), "Хронология") if row[0]
    ]
    assert len(chronology) == 1


async def test_pipeline_refuses_a_package_with_a_foreign_file(tmp_path):
    """S4-03: a file recorded under another run's directory is never read."""
    from claims_assistant.application.package_checks import FOREIGN_FILE

    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    foreign = replace(run.files[0], stored_path="other-run/file.xlsx")
    tampered = replace(run, files=(foreign,))
    provider = CountingProvider(DemoCompanyDataProvider())
    outcome = await pipeline(files, repository, guard(provider)).process(tampered)
    assert outcome.status == RunStatus.FAILED and outcome.failure == FOREIGN_FILE
    assert provider.calls == []


async def test_cross_file_findings_reach_the_report_quality_sheet(tmp_path):
    from claims_assistant.application.check_package import accept_ledger
    from claims_assistant.domain.analysis import FileKind
    from claims_assistant.infrastructure.excel.ledgers import build_debt_history_workbook

    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    reader = OpenpyxlSheetReader()
    accepted_run = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(ROWS),
        repository=repository,
        files=files,
        reader=reader,
    )
    # 1234567894 owes 100.00 in the main file; the history says 120.00 on the same day.
    history = build_debt_history_workbook([["1234567894", DAY, 120.0]])
    await accept_ledger(
        OWNER,
        accepted_run.run.id,
        FileKind.DEBT_HISTORY,
        history,
        coverage=None,
        repository=repository,
        files=files,
        reader=reader,
    )
    await repository.transition(OWNER, accepted_run.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert outcome.status == RunStatus.COMPLETED  # a warning does not make the run partial
    artifact = await repository.get_report(OWNER, run.id)
    quality = sheet_rows(files.read(artifact.stored_path), "Качество данных")
    text = " ".join(str(cell) for row in quality for cell in row if cell is not None)
    assert "расходится с файлом «Контрагенты»" in text


class RateLimitedProvider:
    """Answers «limit» to everything: ``daily_limit`` is how the adapter names Checko's 403
    once the day's requests are spent (#70), ``rate_limited`` a 429 burst."""

    def __init__(self, code: str = "daily_limit") -> None:
        self.calls: list[str] = []
        self.code = code

    async def fetch(self, request: CompanyDataRequest):
        self.calls.append(request.inn)
        return tuple(
            ExternalSnapshot(
                inn=request.inn,
                section=section,
                source="checko",
                mode=DataMode.DEMO,
                fetched_at=NOW,
                status=FetchStatus.RATE_LIMITED,
                coverage=Coverage.UNAVAILABLE,
                missing=("Источник ограничил число запросов.",),
                error=ProviderError(self.code, "Источник ограничил число запросов."),
            )
            for section in request.sections
        )


async def test_a_source_out_of_requests_is_not_asked_for_the_rest_and_the_user_is_told(tmp_path):
    """Free tariff: 100 requests a day, about 33 companies. Past it every company gets 429;
    asking each of them again only burns time, and the user must learn why the check is
    partial and what to do."""
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = RateLimitedProvider()
    outcome = await pipeline(files, repository, guard(provider)).process(run)

    assert outcome.status == RunStatus.PARTIAL
    assert "лимит запросов" in outcome.failure and "завтра" in outcome.failure
    assert provider.calls == ["1234567894"]  # the second company was not asked
    summary = await load_summary(run.id, repository)
    assert summary.source_limited is True
    # Nothing unfinished was saved: a new attempt asks the source again.
    assert await repository.get_step(run.id, "7707083893", FETCH_STEP, FETCH_VERSION) is None


async def test_a_rate_limit_burst_leaves_the_rest_of_the_check_asking(tmp_path):
    """A 429 left after the retries is not the day's limit: the next company is asked, and
    nobody is told to wait until tomorrow (review B on #65)."""
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    provider = RateLimitedProvider("rate_limited")
    outcome = await pipeline(files, repository, guard(provider)).process(run)

    assert provider.calls == ["1234567894", "7707083893"]
    assert "завтра" not in (outcome.failure or "")
    assert (await load_summary(run.id, repository)).source_limited is False


def test_a_summary_saved_before_the_source_limit_existed_still_reads():
    old = (
        '{"companies": 1, "checked": 1, "unchecked": 0, "row_errors": 0, '
        '"priorities": {"low": 1}, "budget_exhausted": false}'
    )
    assert RunSummary.from_payload(old).source_limited is False


# --- a bankruptcy sample cut short on a later page (#71) --------------------------------


class InterruptedBankruptcy:
    """The demo source, but every bankruptcy sample stops on page 2 with ``code``."""

    def __init__(self, code: str) -> None:
        self.inner = DemoCompanyDataProvider()
        self.code = code

    async def fetch(self, request: CompanyDataRequest):
        snapshots = await self.inner.fetch(request)
        return tuple(
            replace(
                snapshot,
                coverage=Coverage.PARTIAL,
                missing=(*snapshot.missing, "Выборка неполная: страница не получена."),
                interrupted=ProviderError(self.code, "сбой на странице 2"),
            )
            if snapshot.section is Section.BANKRUPTCY
            else snapshot
            for snapshot in snapshots
        )


@pytest.mark.parametrize(
    "code, final",
    [("timeout", False), ("network_error", False), ("daily_limit", False), ("api_error", True)],
)
async def test_an_interrupted_sample_is_saved_only_when_a_repeat_would_not_finish_it(
    tmp_path, code, final
):
    """A sample cut by a transient failure or the day's limit is not final: the step is not
    saved, so tomorrow's repeat asks again. A broken answer on page 2 would break again."""
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    await pipeline(files, repository, guard(InterruptedBankruptcy(code))).process(run)
    step = await repository.get_step(run.id, "1234567894", FETCH_STEP, FETCH_VERSION)
    assert (step is not None) is final


async def test_a_company_with_an_unfinished_sample_is_not_counted_as_checked(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    outcome = await pipeline(files, repository, guard(InterruptedBankruptcy("timeout"))).process(
        run
    )
    assert outcome.status == RunStatus.PARTIAL
    assert "Организаций с неполными внешними данными: 2." in outcome.failure


async def test_a_sample_cut_by_the_daily_limit_tells_the_user_to_come_back_tomorrow(tmp_path):
    run, files, repository = await accepted(tmp_path, build_counterparties_template(ROWS))
    outcome = await pipeline(
        files, repository, guard(InterruptedBankruptcy("daily_limit"))
    ).process(run)
    assert (await load_summary(run.id, repository)).source_limited is True
    assert "завтра" in outcome.failure
