"""The analysis pipeline of one run (S3-01): import → external data → rules → report.

The pipeline is the only coordinator: it reads the stored package, asks the guarded
provider for every company, applies the scoring rules, writes the Excel report and records
the outcome. Every expensive step is saved by key (run, INN, step, version) so a run
resumed after an interruption (S3-03) continues from the last saved step instead of
repeating imports or external requests.

The mode is explicit: the pipeline serves runs of exactly one ``DataMode``; a run created
under another mode fails instead of being answered by the wrong provider. A failed
external section stays a failed section in the report — it is never replaced by demo
data. The user gets a summary and, whenever a report was built, the file; a partial
report is still a report.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import DataMode, ExternalSnapshot, FetchStatus, Section
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.report import AnalysisReport, ReportMeta, ReportRow
from claims_assistant.domain.scoring import RULES_VERSION, Priority, assess
from claims_assistant.domain.steps import RUN_SCOPE, StepResult, StepStatus

from .analysis_queue import RunOutcome
from .check_package import FileStorage, StorageError
from .company_data import CompanyDataProvider, CompanyDataRequest
from .external_guard import BUDGET_EXHAUSTED, RunBudget
from .imports import SheetReader, import_counterparties
from .step_payloads import PayloadError, dump_import, dump_snapshots, load_import, load_snapshots
from .step_store import ReportStore, StepStore

logger = logging.getLogger(__name__)

NO_MAIN_FILE = "В пакете нет файла «Контрагенты»."
FILE_UNREADABLE = "Файл пакета недоступен или повреждён; загрузите проверку заново."
NO_USABLE_ROWS = "В файле не осталось пригодных строк; исправьте данные и загрузите заново."
MODE_MISMATCH = "Режим проверки не совпадает с настройкой сервиса; запустите проверку заново."
REPORT_WRITE_FAILED = "Не удалось сохранить файл отчёта; обратитесь к разработчику."
STEP_UNREADABLE = "Сохранённый шаг проверки не читается; запустите проверку заново."

IMPORT_STEP = "import"
IMPORT_VERSION = "counterparties-v2"  # v1 kept only counts; v2 keeps rows and issues
FETCH_STEP = "external_fetch"
FETCH_VERSION = "sections-v1"
REPORT_STEP = "report"
REPORT_VERSION = f"xlsx-v1-rules-{RULES_VERSION}"


class ReportBuilder(Protocol):
    def __call__(self, report: AnalysisReport) -> bytes: ...


class ScopedProviderFactory(Protocol):
    """The guarded provider (S3-02): one shared budget for every company of a run."""

    def scoped(self, budget: RunBudget) -> CompanyDataProvider: ...


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Budget of one run: requests and seconds shared by all its companies."""

    max_requests: int = 1500
    max_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.max_requests <= 0 or self.max_seconds <= 0:
            raise ValueError("Run limits must be positive")


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Safe counts for the user's summary and the outcome text; no INNs or values."""

    companies: int
    checked: int  # every requested section answered OK
    unchecked: int  # at least one section failed, was unavailable or hit the budget
    row_errors: int
    priorities: Mapping[Priority, int]
    budget_exhausted: bool

    def to_payload(self) -> str:
        return json.dumps(
            {
                "companies": self.companies,
                "checked": self.checked,
                "unchecked": self.unchecked,
                "row_errors": self.row_errors,
                "priorities": {key.value: value for key, value in self.priorities.items()},
                "budget_exhausted": self.budget_exhausted,
            }
        )

    @classmethod
    def from_payload(cls, payload: str) -> "RunSummary":
        try:
            data = json.loads(payload)
            return cls(
                companies=int(data["companies"]),
                checked=int(data["checked"]),
                unchecked=int(data["unchecked"]),
                row_errors=int(data["row_errors"]),
                priorities={Priority(key): int(value) for key, value in data["priorities"].items()},
                budget_exhausted=bool(data["budget_exhausted"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise PayloadError("summary payload is not readable") from exc

    def outcome(self) -> RunOutcome:
        reasons: list[str] = []
        if self.row_errors:
            reasons.append(f"Строк с ошибками: {self.row_errors}; они исключены из проверки.")
        if self.budget_exhausted:
            reasons.append("Лимит времени или запросов проверки исчерпан.")
        if self.unchecked:
            reasons.append(f"Организаций с неполными внешними данными: {self.unchecked}.")
        if reasons:
            return RunOutcome(RunStatus.PARTIAL, " ".join(reasons))
        return RunOutcome(RunStatus.COMPLETED)


async def load_summary(run_id: str, steps: StepStore) -> RunSummary | None:
    """The saved summary of a finished pipeline, or None while the report is not built."""
    step = await steps.get_step(run_id, RUN_SCOPE, REPORT_STEP, REPORT_VERSION)
    if step is None or step.status is not StepStatus.OK or step.payload is None:
        return None
    return RunSummary.from_payload(step.payload)


class _Failed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(UTC)


class AnalysisPipeline:
    def __init__(
        self,
        files: FileStorage,
        reader: SheetReader,
        provider: ScopedProviderFactory,
        repository: StepStore | ReportStore,
        *,
        mode: DataMode,
        build_report: ReportBuilder,
        limits: RunLimits = RunLimits(),
        sections: tuple[Section, ...] = tuple(Section),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._files = files
        self._reader = reader
        self._provider = provider
        self._repository = repository
        self._mode = mode
        self._build_report = build_report
        self._limits = limits
        self._sections = sections
        self._clock = clock

    async def process(self, run: AnalysisRun) -> RunOutcome:
        if run.mode is not self._mode:
            logger.error(
                "run_mode_mismatch run_id=%s run=%s service=%s", run.id, run.mode, self._mode
            )
            return RunOutcome(RunStatus.FAILED, MODE_MISMATCH)
        try:
            summary = await load_summary(run.id, self._repository)
            if summary is not None:
                return summary.outcome()
            rows, issues = await self._import(run)
            snapshots = await self._fetch_all(run, rows)
            summary = await self._report(run, rows, issues, snapshots)
        except _Failed as failed:
            return RunOutcome(RunStatus.FAILED, failed.reason)
        except PayloadError:
            logger.error("run_step_unreadable run_id=%s", run.id)
            return RunOutcome(RunStatus.FAILED, STEP_UNREADABLE)
        return summary.outcome()

    # --- import -------------------------------------------------------------------

    async def _import(
        self, run: AnalysisRun
    ) -> tuple[tuple[CounterpartyRow, ...], tuple[ImportIssue, ...]]:
        saved = await self._repository.get_step(run.id, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION)
        if saved is not None:
            if saved.status is StepStatus.FAILED:
                raise _Failed(saved.error)
            return load_import(saved.payload or "")
        try:
            rows, issues = await self._read_package(run)
        except _Failed as failed:
            await self._save(run, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION, error=failed.reason)
            raise
        await self._save(
            run, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION, payload=dump_import(rows, issues)
        )
        return rows, issues

    async def _read_package(
        self, run: AnalysisRun
    ) -> tuple[tuple[CounterpartyRow, ...], tuple[ImportIssue, ...]]:
        main = [file for file in run.files if file.kind == FileKind.COUNTERPARTIES]
        if not main:
            raise _Failed(NO_MAIN_FILE)
        try:
            data = await asyncio.to_thread(self._files.read, main[0].stored_path)
        except StorageError:
            raise _Failed(FILE_UNREADABLE) from None
        result = await asyncio.to_thread(
            import_counterparties, self._reader, data, analysis_date=run.analysis_date
        )
        if not result.rows:
            raise _Failed(NO_USABLE_ROWS)
        return result.rows, result.issues

    # --- external data ------------------------------------------------------------

    async def _fetch_all(
        self, run: AnalysisRun, rows: tuple[CounterpartyRow, ...]
    ) -> dict[str, tuple[ExternalSnapshot, ...]]:
        budget = RunBudget.for_run(
            self._limits.max_requests, self._limits.max_seconds, self._clock()
        )
        provider = self._provider.scoped(budget)
        result: dict[str, tuple[ExternalSnapshot, ...]] = {}
        for row in rows:
            saved = await self._repository.get_step(run.id, row.inn, FETCH_STEP, FETCH_VERSION)
            if saved is not None and saved.status is StepStatus.OK:
                result[row.inn] = load_snapshots(saved.payload or "")
                continue
            request = CompanyDataRequest(inn=row.inn, sections=self._sections)
            snapshots = await provider.fetch(request)
            result[row.inn] = snapshots
            # A budget placeholder is not a result: left unsaved, a resumed run retries it.
            if not any(_hit_budget(snapshot) for snapshot in snapshots):
                await self._save(
                    run, row.inn, FETCH_STEP, FETCH_VERSION, payload=dump_snapshots(snapshots)
                )
        return result

    # --- rules and report ---------------------------------------------------------

    async def _report(
        self,
        run: AnalysisRun,
        rows: tuple[CounterpartyRow, ...],
        issues: tuple[ImportIssue, ...],
        snapshots: dict[str, tuple[ExternalSnapshot, ...]],
    ) -> RunSummary:
        report_rows = tuple(
            ReportRow(
                counterparty=row,
                assessment=assess(
                    row.inn, snapshots[row.inn], row, analysis_date=run.analysis_date
                ),
                snapshots=snapshots[row.inn],
            )
            for row in rows
        )
        fetched = [
            s.fetched_at for group in snapshots.values() for s in group if not _hit_budget(s)
        ]
        meta = ReportMeta(
            run_id=run.id,
            analysis_date=run.analysis_date,
            mode=run.mode,
            created_at=self._clock(),
            checked_at=max(fetched) if fetched else None,
            package=(f"Контрагенты: {len(rows)} строк",),
        )
        report = AnalysisReport(meta=meta, rows=report_rows, import_issues=issues)
        data = await asyncio.to_thread(self._build_report, report)
        try:
            stored_path = await asyncio.to_thread(self._files.save, run.id, data)
        except StorageError:
            logger.error("report_write_failed run_id=%s", run.id)
            raise _Failed(REPORT_WRITE_FAILED) from None
        await self._repository.save_report(run.id, stored_path)
        summary = _summarize(report_rows, issues)
        await self._save(run, RUN_SCOPE, REPORT_STEP, REPORT_VERSION, payload=summary.to_payload())
        return summary

    async def _save(
        self,
        run: AnalysisRun,
        inn: str,
        step: str,
        version: str,
        *,
        payload: str | None = None,
        error: str | None = None,
    ) -> None:
        await self._repository.save_step(
            StepResult(
                run_id=run.id,
                inn=inn,
                step=step,
                version=version,
                status=StepStatus.FAILED if error else StepStatus.OK,
                completed_at=self._clock(),
                payload=payload,
                error=error,
            )
        )


def _hit_budget(snapshot: ExternalSnapshot) -> bool:
    return snapshot.error is not None and snapshot.error.code == BUDGET_EXHAUSTED


def _summarize(rows: tuple[ReportRow, ...], issues: tuple[ImportIssue, ...]) -> RunSummary:
    priorities = {priority: 0 for priority in Priority}
    checked = 0
    budget_exhausted = False
    for row in rows:
        priorities[row.assessment.priority] += 1
        if all(snapshot.status is FetchStatus.OK for snapshot in row.snapshots):
            checked += 1
        if any(_hit_budget(snapshot) for snapshot in row.snapshots):
            budget_exhausted = True
    return RunSummary(
        companies=len(rows),
        checked=checked,
        unchecked=len(rows) - checked,
        row_errors=sum(1 for issue in issues if issue.severity is IssueSeverity.ERROR),
        priorities=priorities,
        budget_exhausted=budget_exhausted,
    )
