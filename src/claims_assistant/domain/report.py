"""S2-05: input model of the Excel report generator (docs/data-contracts.md, «Выходной Excel»).

One ``AnalysisReport`` per run. Each row joins a «Контрагенты» line with its priority
``Assessment`` (S3-05) and the external snapshots the assessment rests on, so the generator
can fill all four sheets without calling anything: «Приоритеты» from the assessments,
«Основания» from the facts and their evidence, «Качество данных» from import issues and
``missing_data``, «О проверке» from ``ReportMeta``.

A demo report may carry predefined demo priorities (control package, S2-06) instead of
rule results; ``ReportMeta.demo_scores`` marks that, and it is only allowed in demo mode.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from claims_assistant.domain.analysis import FileKind, UploadedFile
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import DataMode, ExternalSnapshot
from claims_assistant.domain.imports import ImportIssue
from claims_assistant.domain.scoring import RULES_VERSION, Assessment, Priority

DEMO_SCORE_NOTE = "Демонстрационная оценка задана заранее; правила не применялись."

FILE_KIND_TITLES = {
    FileKind.COUNTERPARTIES: "Контрагенты",
    FileKind.PAYMENTS: "Платежи",
    FileKind.DEBT_HISTORY: "История долга",
    FileKind.INTERACTIONS: "Взаимодействия",
}


def file_labels(files: Sequence[UploadedFile]) -> tuple[tuple[str, str], ...]:
    """(file id, label) in upload order: the kind, the payments period and, when a kind
    repeats, the file's number — «Платежи за 01.06.2026–30.06.2026, файл 2».

    The label names what the user chose and typed, never the file's contents; the upload
    time is left out, as it would need the user's time zone.
    """
    total: dict[FileKind, int] = {}
    for file in files:
        total[file.kind] = total.get(file.kind, 0) + 1
    seen: dict[FileKind, int] = {}
    labels = []
    for file in files:
        seen[file.kind] = seen.get(file.kind, 0) + 1
        label = FILE_KIND_TITLES.get(file.kind, file.kind.value)
        if file.coverage is not None:
            start, end = file.coverage.start, file.coverage.end
            label += f" за {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"
        if total[file.kind] > 1:
            label += f", файл {seen[file.kind]}"
        labels.append((file.id, label))
    return tuple(labels)


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class ReportMeta:
    run_id: str
    analysis_date: date
    mode: DataMode
    created_at: datetime
    # The oldest external answer in the report (cached answers keep their own time): every
    # external fact is at least this fresh. None if no source was queried.
    checked_at: datetime | None = None
    rules_version: str = RULES_VERSION
    ai_version: str | None = None  # no AI explanations yet (sprint 5)
    package: tuple[str, ...] = ()  # human-readable composition, e.g. "Контрагенты: 50 строк"
    # (file id, label) of the package files, so «Качество данных» names the file of an
    # issue (``ImportIssue.file_id``); see ``file_labels``.
    files: tuple[tuple[str, str], ...] = ()
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
