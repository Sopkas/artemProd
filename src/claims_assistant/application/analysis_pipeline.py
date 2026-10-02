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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from claims_assistant.domain.ai_context import ContextLimits, build_context, versions
from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_report import SHEET_NAME as DEBT_REPORT_SHEET
from claims_assistant.domain.external import (
    DataMode,
    ExternalSnapshot,
    FactKind,
    FetchStatus,
    Section,
)
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.indicators import package_indicators
from claims_assistant.domain.interactions import InteractionRow, chronology
from claims_assistant.domain.plural import of_companies as _companies
from claims_assistant.domain.report import AnalysisReport, ReportMeta, ReportRow, file_labels
from claims_assistant.domain.scoring import RULES_VERSION, Assessment, Priority, assess
from claims_assistant.domain.steps import RUN_SCOPE, StepResult, StepStatus

from .ai_guard import AiBudget, AiRunLimits, ScopedExplainerFactory
from .ai_spend import NO_MONEY_LEFT, AiSpendStore, MonthlyLimit, month_of
from .analysis_queue import RunOutcome
from .analysis_repository import RepositoryError
from .check_package import FileStorage, StorageError
from .company_data import CompanyDataProvider, CompanyDataRequest
from .contract_link import LinkedContracts, link_contracts
from .explanations import EXPLANATION_STEP, StoredExplanation
from .external_guard import BUDGET_EXHAUSTED, TRANSIENT_CODES, RunBudget
from .imports import SheetReader
from .internal_context import payment_periods
from .package_checks import PackageIntegrityError, PackageReview, review_package
from .recommendation import (
    AiLimits,
    RecommendationProvider,
    explanation_key,
    request_explanation,
)
from .step_payloads import (
    ImportedPackage,
    PayloadError,
    dump_import,
    dump_links,
    dump_snapshots,
    load_import,
    load_snapshots,
)
from .step_store import ReportStore, StepStore

logger = logging.getLogger(__name__)

NO_MAIN_FILE = "В пакете нет файла «Контрагенты»."
FILE_UNREADABLE = "Файл пакета недоступен или повреждён; загрузите проверку заново."
NO_USABLE_ROWS = "В файле не осталось пригодных строк; исправьте данные и загрузите заново."
MODE_MISMATCH = "Режим проверки не совпадает с настройкой сервиса; запустите проверку заново."
REPORT_WRITE_FAILED = "Не удалось сохранить файл отчёта; обратитесь к разработчику."
STEP_UNREADABLE = "Сохранённый шаг проверки не читается; запустите проверку заново."

IMPORT_STEP = "import"
LINKS_STEP = "contracts_link"
LINKS_VERSION = "links-v1"
# v1: counts; v2: rows and issues; v3: every file; v4: contracts; v5: rows per file, and
# a company printed twice in one overdue report is one company.
IMPORT_VERSION = "package-v5"
FETCH_STEP = "external_fetch"
FETCH_VERSION = "sections-v1"
REPORT_STEP = "report"
REPORT_VERSION = f"xlsx-v1-rules-{RULES_VERSION}"


class PipelineRepository(StepStore, ReportStore, Protocol):
    """Step results and the report artifact of one storage."""


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
    # S5-03: companies left without an accepted AI explanation while a provider was
    # configured (model unavailable, answer rejected, AI budget exhausted).
    explanations_missing: int = 0
    # S5-03: the month's money limit was already reached, so the model was not asked at
    # all. Kept apart from the count above: «не спрашивали» and «спросили, не вышло» are
    # different things for whoever reads the summary.
    ai_month_exhausted: bool = False

    def to_payload(self) -> str:
        return json.dumps(
            {
                "companies": self.companies,
                "checked": self.checked,
                "unchecked": self.unchecked,
                "row_errors": self.row_errors,
                "priorities": {key.value: value for key, value in self.priorities.items()},
                "budget_exhausted": self.budget_exhausted,
                "explanations_missing": self.explanations_missing,
                "ai_month_exhausted": self.ai_month_exhausted,
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
                explanations_missing=int(data.get("explanations_missing", 0)),
                ai_month_exhausted=bool(data.get("ai_month_exhausted", False)),
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
        if self.ai_month_exhausted:
            reasons.append(NO_MONEY_LEFT)
        elif self.explanations_missing:
            reasons.append(
                f"Пояснений ИИ нет у {self.explanations_missing} организаций; "
                "в отчёте — рекомендация по правилам."
            )
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
        repository: PipelineRepository,
        *,
        mode: DataMode,
        build_report: ReportBuilder,
        limits: RunLimits = RunLimits(),
        sections: tuple[Section, ...] = tuple(Section),
        clock: Callable[[], datetime] = _now,
        explainer: RecommendationProvider | ScopedExplainerFactory | None = None,
        ai_limits: AiLimits = AiLimits(),
        ai_run_limits: AiRunLimits = AiRunLimits(),
        ai_send_comments: bool = False,
        ai_spend: AiSpendStore | None = None,
        ai_month_limit_rub: Decimal | None = None,
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
        # S5-02: with a provider, every assessment gets an explanation attempt whose
        # answer is stored by version; without one the report keeps the rules' next step.
        self._explainer = explainer
        self._ai_limits = ai_limits
        # S5-03: the month's spend lives in storage, so it survives restarts and is shared
        # by every check; the run's own money limit is in ``ai_run_limits``.
        self._ai_spend = ai_spend
        self._ai_month_limit = ai_month_limit_rub
        self._ai_month_exhausted = False
        self._ai_run_limits = ai_run_limits
        self._ai_send_comments = ai_send_comments

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
            package = await self._import(run)
            snapshots = await self._fetch_all(run, package.rows)
            summary = await self._report(run, package, snapshots)
        except _Failed as failed:
            return RunOutcome(RunStatus.FAILED, failed.reason)
        except PayloadError:
            logger.error("run_step_unreadable run_id=%s", run.id)
            return RunOutcome(RunStatus.FAILED, STEP_UNREADABLE)
        return summary.outcome()

    # --- import -------------------------------------------------------------------

    async def _import(self, run: AnalysisRun) -> ImportedPackage:
        """Every file of the package, from the saved step or read now and saved (S4-04)."""
        saved = await self._repository.get_step(run.id, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION)
        if saved is not None:
            if saved.status is StepStatus.FAILED:
                raise _Failed(saved.error)
            return load_import(saved.payload or "")
        try:
            review = await self._read_package(run)
        except _Failed as failed:
            await self._save(run, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION, error=failed.reason)
            raise
        package = ImportedPackage(
            rows=review.counterparties,
            issues=review.issues,
            payments=review.payments,
            history=review.history,
            interactions=review.interactions,
            contracts=review.contracts,
            file_rows=dict(review.file_rows),
        )
        await self._save(run, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION, payload=dump_import(package))
        return package

    async def _read_package(self, run: AnalysisRun) -> PackageReview:
        """The whole package (S4-03): every file is checked on its own and against the
        others; the issues go to the report's «Качество данных»."""
        if not any(file.kind == FileKind.COUNTERPARTIES for file in run.files):
            raise _Failed(NO_MAIN_FILE)
        try:
            review = await review_package(run, self._files, self._reader)
        except PackageIntegrityError as error:
            logger.error("package_integrity run_id=%s", run.id)
            raise _Failed(str(error)) from None
        except StorageError:
            raise _Failed(FILE_UNREADABLE) from None
        if review.blocking:
            raise _Failed(review.blocking[0].reason)
        if not review.counterparties:
            raise _Failed(NO_USABLE_ROWS)
        return review

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
            # A budget placeholder or a transient failure (after the guard's retries) is
            # not a final result: left unsaved, a resumed run asks the source again.
            if not any(_is_open(snapshot) for snapshot in snapshots):
                await self._save(
                    run, row.inn, FETCH_STEP, FETCH_VERSION, payload=dump_snapshots(snapshots)
                )
        return result

    # --- rules and report ---------------------------------------------------------

    async def _report(
        self,
        run: AnalysisRun,
        package: ImportedPackage,
        snapshots: dict[str, tuple[ExternalSnapshot, ...]],
    ) -> RunSummary:
        rows, issues = package.rows, package.issues
        # Internal indicators (S4-06) from the owner's own files: payments with the periods
        # they vouched for, debt history, and the finances section already fetched.
        finances = {
            inn: next((s for s in group if s.section is Section.FINANCES), None)
            for inn, group in snapshots.items()
        }
        indicators = package_indicators(
            rows,
            run.analysis_date,
            package.payments,
            payment_periods(run),
            package.history,
            {inn: snapshot for inn, snapshot in finances.items() if snapshot is not None},
        )
        assessments = {
            row.inn: assess(
                row.inn,
                snapshots[row.inn],
                row,
                analysis_date=run.analysis_date,
                indicators=indicators[row.inn],
            )
            for row in rows
        }
        known = {row.inn for row in rows}
        interactions = tuple(item for item in package.interactions if item.inn in known)
        # S7-01: the overdue report names companies, not INNs — the link is by name, and
        # what did not match is a finding of the report, not a silent loss.
        linked = link_contracts(package.contracts, _company_names(rows, snapshots))
        issues = issues + _contract_issues(linked)
        if package.contracts:
            # Saved so the card shows the same answer instead of working it out again
            # from one company's name, where two namesakes look alike (review B on #58).
            await self._save(
                run, RUN_SCOPE, LINKS_STEP, LINKS_VERSION, payload=dump_links(linked.by_inn)
            )
        explanations = await self._explain(
            run, rows, assessments, indicators, snapshots, chronology(interactions)
        )
        report_rows = tuple(
            ReportRow(
                counterparty=row,
                assessment=assessments[row.inn],
                snapshots=snapshots[row.inn],
                indicators=indicators[row.inn],
                explanation=explanations[row.inn].explanation if row.inn in explanations else None,
                explanation_status=explanations[row.inn].status
                if row.inn in explanations
                else None,
            )
            for row in rows
        )
        # The oldest answer among the sections: cached snapshots keep their own
        # fetched_at, so every external fact in the report is at least this fresh.
        fetched = [
            s.fetched_at for group in snapshots.values() for s in group if not _hit_budget(s)
        ]
        meta = ReportMeta(
            run_id=run.id,
            analysis_date=run.analysis_date,
            mode=run.mode,
            created_at=self._clock(),
            checked_at=min(fetched) if fetched else None,
            package=_package_lines(run, package),
            files=file_labels(run.files),
            ai_version=self._ai_version(),
        )
        report = AnalysisReport(
            meta=meta,
            rows=report_rows,
            import_issues=issues,
            interactions=interactions,
            contracts=linked.by_inn,
        )
        data = await asyncio.to_thread(self._build_report, report)
        previous = await self._repository.get_report(run.owner_id, run.id)
        try:
            stored_path = await asyncio.to_thread(self._files.save, run.id, data)
        except StorageError:
            logger.error("report_write_failed run_id=%s", run.id)
            raise _Failed(REPORT_WRITE_FAILED) from None
        await self._repository.save_report(run.id, stored_path)
        if previous is not None and previous.stored_path != stored_path:
            # A report built just before a crash is replaced, not left behind as an orphan.
            try:
                await asyncio.to_thread(self._files.remove, previous.stored_path)
            except StorageError:
                logger.warning("report_orphan run_id=%s", run.id)
        summary = _summarize(
            report_rows,
            issues,
            explainer=self._explainer is not None,
            month_exhausted=self._ai_month_exhausted,
        )
        await self._save(run, RUN_SCOPE, REPORT_STEP, REPORT_VERSION, payload=summary.to_payload())
        return summary

    # --- AI explanations (S5-02) -----------------------------------------------------

    def _ai_version(self) -> str | None:
        if self._explainer is None:
            return None
        return f"{self._explainer.name} {self._explainer.model}; {versions()}"

    async def _explain(
        self,
        run: AnalysisRun,
        rows: tuple[CounterpartyRow, ...],
        assessments: dict[str, Assessment],
        indicators: dict,
        snapshots: dict[str, tuple[ExternalSnapshot, ...]],
        interactions: dict[str, tuple[InteractionRow, ...]],
    ) -> dict[str, StoredExplanation]:
        """One explanation attempt per company, each answer stored under its versions.

        A stored final answer (accepted or rejected) is read back instead of asking
        again; a stored answer whose versions changed is simply a different key, so the
        model is asked once per version. Unavailable answers are not stored: the next
        run of the pipeline may try again (S5-03 bounds that).
        """
        outcomes: dict[str, StoredExplanation] = {}
        self._ai_month_exhausted = False
        if self._explainer is None:
            return outcomes
        limits_for_run = await self._money_for_this_run()
        if limits_for_run is None:
            # The month the customer agreed on is spent: the model is not asked at all,
            # and the report says so instead of showing an unexplained gap (S5-03).
            self._ai_month_exhausted = True
            logger.info("ai_month_exhausted run_id=%s", run.id)
            return outcomes
        # S5-03: a guarded provider gets one budget for the whole run; once it is spent
        # the remaining companies get no explanation and the check still finishes.
        explainer = self._explainer
        budget = AiBudget.for_run(limits_for_run, self._clock())
        if isinstance(explainer, ScopedExplainerFactory):
            explainer = explainer.scoped(budget)
        limits = ContextLimits(include_comments=self._ai_send_comments)
        for index, row in enumerate(rows, start=1):
            context = build_context(
                row,
                assessments[row.inn],
                run.analysis_date,
                indicators=indicators[row.inn],
                interactions=interactions.get(row.inn, ()),
                snapshots=snapshots[row.inn],
                limits=limits,
                reference=f"row-{index}",
            )
            stored = await self._stored_explanation(run, row.inn, context)
            if stored is None:
                outcome = await request_explanation(explainer, context, self._ai_limits)
                stored = StoredExplanation.from_outcome(outcome, f"row-{index}")
                if stored.final:
                    await self._save(
                        run, row.inn, EXPLANATION_STEP, stored.versions, payload=stored.to_payload()
                    )
            outcomes[row.inn] = stored
        await self._record_spend(budget)
        return outcomes

    async def _money_for_this_run(self) -> AiRunLimits | None:
        """The run's limits with the money left this month, or None when none is left.

        The month is read once, before the first question: a check that starts within the
        limit is allowed to finish, and may overshoot the month by at most one run's
        limit. Taking a lock around every call would buy exactness we do not need at
        1000 ₽ a month, and would make one slow answer block the others.
        """
        limits = self._ai_run_limits
        if self._ai_spend is None or self._ai_month_limit is None:
            return limits
        spent = await self._ai_spend.spent(month_of(self._clock()))
        month = MonthlyLimit(limit_rub=self._ai_month_limit, spent_rub=spent)
        if month.reached:
            return None
        left = month.left_rub
        if limits.max_rub is not None:
            left = min(left, limits.max_rub)
        return replace(limits, max_rub=left)

    async def _record_spend(self, budget: AiBudget) -> None:
        """Add what this check spent to the month — and never fail the check over it.

        Reading the counter before the run is fatal on purpose: not knowing the balance
        means not spending. Writing afterwards is the opposite case — the money is gone
        whatever we do, and refusing to record it would also take away the report the
        user has already paid for. So a write failure is loud in the journal and nothing
        more (review B on #59).
        """
        if self._ai_spend is None or budget.spent_rub <= 0:
            return
        try:
            total = await self._ai_spend.add(month_of(self._clock()), budget.spent_rub)
        except RepositoryError:
            logger.error("ai_spend_not_recorded run_rub=%s", format(budget.spent_rub, "f"))
            return
        logger.info(
            "ai_spend run_rub=%s month_rub=%s", format(budget.spent_rub, "f"), format(total, "f")
        )

    async def _stored_explanation(self, run, inn: str, context) -> StoredExplanation | None:
        key = explanation_key(self._explainer, context)
        step = await self._repository.get_step(run.id, inn, EXPLANATION_STEP, key)
        if step is None or step.status is not StepStatus.OK or step.payload is None:
            return None
        return StoredExplanation.from_payload(step.payload)

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


def _company_names(
    rows: tuple[CounterpartyRow, ...], snapshots: dict[str, tuple[ExternalSnapshot, ...]]
) -> dict[str, str | None]:
    """INN → the company name the source gave, or None when it gave none (S7-01)."""
    names: dict[str, str | None] = {}
    for row in rows:
        name = None
        for snapshot in snapshots.get(row.inn, ()):
            for fact in snapshot.facts:
                if fact.kind is FactKind.COMPANY_NAME and isinstance(fact.value, str):
                    name = fact.value
                    break
        names[row.inn] = name
    return names


def _contract_issues(linked: LinkedContracts) -> tuple[ImportIssue, ...]:
    """What the overdue report said about companies we could not recognise."""
    issues = []
    if linked.unknown:
        issues.append(
            _issue(
                "contracts_owner_unknown",
                f"Договоры {_companies(len(linked.unknown))} из отчёта по договорам "
                "не отнесены ни к одной организации проверки: названия не совпали.",
            )
        )
    if linked.ambiguous:
        issues.append(
            _issue(
                "contracts_owner_ambiguous",
                f"Договоры {_companies(len(linked.ambiguous))} не отнесены: "
                "в проверке есть несколько организаций с таким же названием.",
            )
        )
    if linked.without_form:
        issues.append(
            _issue(
                "contracts_matched_without_form",
                f"Договоры {_companies(len(linked.without_form))} привязаны по названию "
                "без учёта правовой формы — проверьте, те ли это организации.",
            )
        )
    if linked.unnamed:
        issues.append(
            _issue(
                "contracts_name_missing",
                f"У {_companies(len(linked.unnamed))} нет названия от источника, "
                "поэтому договоры из отчёта к ним не привязывались.",
            )
        )
    return tuple(issues)


def _issue(code: str, reason: str) -> ImportIssue:
    return ImportIssue(
        code=code, severity=IssueSeverity.WARNING, sheet=DEBT_REPORT_SHEET, reason=reason
    )


def _hit_budget(snapshot: ExternalSnapshot) -> bool:
    return snapshot.error is not None and snapshot.error.code == BUDGET_EXHAUSTED


def _is_open(snapshot: ExternalSnapshot) -> bool:
    """Not a final answer: the budget placeholder or a transient source failure."""
    if _hit_budget(snapshot) or snapshot.status is FetchStatus.RATE_LIMITED:
        return True
    return snapshot.error is not None and snapshot.error.code in TRANSIENT_CODES


_EXTRA_FILE_LABELS = {
    FileKind.PAYMENTS: "Платежи",
    FileKind.DEBT_HISTORY: "История долга",
    FileKind.INTERACTIONS: "Взаимодействия",
}


_FILE_USE = {
    FileKind.PAYMENTS: "давность платежа",
    FileKind.DEBT_HISTORY: "динамика долга за месяц",
    FileKind.INTERACTIONS: "лист «Хронология»",
    FileKind.DEBT_REPORT: "договоры в карточке и на листе «Договоры»",
}


def _package_lines(run: AnalysisRun, package: ImportedPackage) -> tuple[str, ...]:
    """The package as the report states it: every file with the same label as on
    «Качество данных», how many usable rows it gave and what it was used for (S4-04)."""
    rows, issues = package.rows, package.issues
    bad_rows = {
        issue.row
        for issue in issues
        if issue.severity is IssueSeverity.ERROR and issue.row is not None
    }
    lines = []
    for file, (_, label) in zip(run.files, file_labels(run.files), strict=True):
        if file.kind is FileKind.COUNTERPARTIES:
            lines.append(f"{label}: {len(rows) + len(bad_rows)} строк, пригодных {len(rows)}")
        else:
            # Each file is credited with what it gave itself (block 5, 30.09.2026: every
            # payments file showed the total of all of them, the report showed zero).
            use = _FILE_USE.get(file.kind, "не используется")
            usable = package.file_rows.get(file.id, 0)
            unit = "договоров" if file.kind is FileKind.DEBT_REPORT else "пригодных строк"
            lines.append(f"{label}: использован — {use}; {unit}: {usable}")
    return tuple(lines)


def _summarize(
    rows: tuple[ReportRow, ...],
    issues: tuple[ImportIssue, ...],
    *,
    explainer: bool = False,
    month_exhausted: bool = False,
) -> RunSummary:
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
        ai_month_exhausted=month_exhausted,
        explanations_missing=(
            sum(1 for row in rows if row.explanation is None) if explainer else 0
        ),
    )
