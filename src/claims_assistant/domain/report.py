"""S2-05: input model of the Excel report generator (docs/data-contracts.md, «Выходной Excel»).

One ``AnalysisReport`` per run. Each row joins a «Контрагенты» line with its priority
``Assessment`` (S3-05) and the external snapshots the assessment rests on, so the generator
can fill all four sheets without calling anything: «Приоритеты» from the assessments,
«Основания» from the facts and their evidence, «Качество данных» from import issues and
``missing_data``, «О проверке» from ``ReportMeta``.

A demo report may carry predefined demo priorities (control package, S2-06) instead of
rule results; ``ReportMeta.demo_scores`` marks that, and it is only allowed in demo mode.
"""

from dataclasses import dataclass
from datetime import date, datetime

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import DataMode, ExternalSnapshot
from claims_assistant.domain.imports import ImportIssue
from claims_assistant.domain.scoring import RULES_VERSION, Assessment, Priority

DEMO_SCORE_NOTE = "Демонстрационная оценка задана заранее; правила не применялись."


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class ReportMeta:
    run_id: str
    analysis_date: date
    mode: DataMode
    created_at: datetime
    checked_at: datetime | None = None  # when external sources were queried, if they were
    rules_version: str = RULES_VERSION
    ai_version: str | None = None  # no AI explanations yet (sprint 5)
    package: tuple[str, ...] = ()  # human-readable composition, e.g. "Контрагенты: 50 строк"
    demo_scores: bool = False

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("Run id is required")
        if type(self.analysis_date) is not date:
            raise ValueError("Analysis date must be a calendar date")
        if not isinstance(self.mode, DataMode):
            raise ValueError("Mode must be a DataMode")
        _aware(self.created_at, "created_at")
        _aware(self.checked_at, "checked_at")
        if self.demo_scores and self.mode is not DataMode.DEMO:
            raise ValueError("Predefined demo priorities are only allowed in demo mode")


@dataclass(frozen=True, slots=True)
class ReportRow:
    counterparty: CounterpartyRow
    assessment: Assessment
    snapshots: tuple[ExternalSnapshot, ...] = ()

    def __post_init__(self) -> None:
        inn = self.counterparty.inn
        if self.assessment.inn != inn or any(s.inn != inn for s in self.snapshots):
            raise ValueError("Report row mixes different INNs")


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    meta: ReportMeta
    rows: tuple[ReportRow, ...] = ()
    import_issues: tuple[ImportIssue, ...] = ()

    def __post_init__(self) -> None:
        inns = [row.counterparty.inn for row in self.rows]
        if len(set(inns)) != len(inns):
            raise ValueError("One row per INN")


def demo_assessment(inn: str, priority: Priority) -> Assessment:
    """A predefined demo priority shaped as an assessment; nothing is computed."""
    return Assessment(
        inn=inn,
        priority=priority,
        signals=(),
        missing_data=(DEMO_SCORE_NOTE,),
        coverage={},
        base_complete=False,
    )
