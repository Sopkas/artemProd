"""S3-05: external priority rules and the overall priority assembly (docs/scoring.md v0.1).

Pure domain code: no network, files or clock. Inputs are the normalized section snapshots
of one INN and, optionally, its «Контрагенты» row. Missing data is never read as zero: an
indicator that cannot be checked goes to ``missing_data`` and, without any signal, the
result is ``unknown`` («недостаточно данных»), not ``low``.

Internal signals (overdue, payments, debt growth) arrive in S4-07 through
``internal_signals``; the assembly already treats every signal the same way.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    Section,
)

RULES_VERSION = "0.1"
REVENUE_DROP_PERCENT = Decimal("30")


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


# Report order: critical → high → medium → unknown → low (docs/scoring.md).
_REPORT_RANK = {
    Priority.CRITICAL: 0,
    Priority.HIGH: 1,
    Priority.MEDIUM: 2,
    Priority.UNKNOWN: 3,
    Priority.LOW: 4,
}

_SECTION_LABELS = {
    Section.COMPANY: "Сведения об организации",
    Section.BANKRUPTCY: "Сообщения ЕФРСБ",
    Section.FINANCES: "Финансовая отчётность",
}


@dataclass(frozen=True, slots=True)
class Signal:
    """One rule that fired: its code, minimal priority and the facts it rests on."""

    code: str
    level: Priority
    reason: str
    fact_ids: tuple[str, ...] = ()
    value: str | None = None
    observed_on: date | None = None

    def __post_init__(self) -> None:
        if self.level in (Priority.LOW, Priority.UNKNOWN):
            raise ValueError("A signal raises priority; low/unknown are outcomes, not signals")
        if not self.reason.strip():
            raise ValueError("A signal needs a reason")


@dataclass(frozen=True, slots=True)
class Assessment:
    inn: str
    priority: Priority
    signals: tuple[Signal, ...]
    missing_data: tuple[str, ...]
    coverage: dict[str, Coverage]
    base_complete: bool
    rules_version: str = RULES_VERSION

    @property
    def next_step(self) -> str:
        """Base recommendation by rule; never a legal instruction or deadline."""
        codes = {signal.code for signal in self.signals}
        if codes & {"confirmed_procedure", "unresolved_bankruptcy_event"}:
            return "Проверить событие и документы по указанному источнику."
        if "liquidation_status" in codes:
            return "Проверить статус организации и документы по указанному источнику."
        if codes & {"overdue_60", "overdue_30", "no_payments_60", "debt_doubled"}:
            return "Уточнить причины задержки и договорённости об оплате."
        if codes & {"revenue_drop_30", "net_loss"}:
            return "Уточнить финансовое положение контрагента и условия оплаты."
        if self.priority is Priority.UNKNOWN:
            return "Дополнить данные: сведений для оценки недостаточно."
        return "Работать в обычном порядке."


def combine(signals: Sequence[Signal], base_complete: bool) -> Priority:
    """The final rule: gaps never lower a priority that known facts already justify."""
    levels = [signal.level for signal in signals]
    if Priority.CRITICAL in levels:
        return Priority.CRITICAL
    medium_codes = {signal.code for signal in signals if signal.level is Priority.MEDIUM}
    if Priority.HIGH in levels or len(medium_codes) >= 2:
        return Priority.HIGH
    if medium_codes:
        return Priority.MEDIUM
    return Priority.LOW if base_complete else Priority.UNKNOWN


def _facts(snapshot: ExternalSnapshot | None, kind: FactKind) -> list[Fact]:
    return [] if snapshot is None else [f for f in snapshot.facts if f.kind is kind]


def _company_signals(snapshot: ExternalSnapshot | None) -> list[Signal]:
    signals = []
    for fact in _facts(snapshot, FactKind.COMPANY_STATUS):
        if fact.value in (CompanyStatus.LIQUIDATING, CompanyStatus.LIQUIDATED):
            signals.append(
                Signal(
                    "liquidation_status",
                    Priority.CRITICAL,
                    "Статус организации — ликвидация или её начало.",
                    (fact.id,),
                    value=str(fact.value),
                    observed_on=fact.observed_on,
                )
            )
    return signals


def _bankruptcy_signals(snapshot: ExternalSnapshot | None) -> list[Signal]:
    events = _facts(snapshot, FactKind.BANKRUPTCY_EVENT)
    if not events:
        return []
    # Types are not classified against a verified dictionary yet, so a confirmed procedure
    # is never inferred from text; every message needs review. Repeated messages form one
    # signal, not several (docs/scoring.md).
    latest = max((f.observed_on for f in events if f.observed_on), default=None)
    return [
        Signal(
            "unresolved_bankruptcy_event",
            Priority.HIGH,
            "Есть сообщения ЕФРСБ о банкротстве; смысл и актуальность требуют проверки.",
            tuple(f.id for f in events),
            value=str(len(events)),
            observed_on=latest,
        )
    ]


def _by_year(facts: Iterable[Fact]) -> dict[int, Fact]:
    return {f.period.end.year: f for f in facts if f.period is not None}


def _finance_signals(snapshot: ExternalSnapshot | None) -> list[Signal]:
    signals = []
    revenue = _by_year(_facts(snapshot, FactKind.REVENUE))
    pair = next((y for y in sorted(revenue, reverse=True) if y - 1 in revenue), None)
    if pair is not None:
        new, prev = revenue[pair], revenue[pair - 1]
        # Percent change only for a positive base and matching units; otherwise no trend.
        if (
            isinstance(new.value, Decimal)
            and isinstance(prev.value, Decimal)
            and prev.value > 0
            and new.unit == prev.unit
        ):
            change = (new.value - prev.value) / prev.value * 100
            if change <= -REVENUE_DROP_PERCENT:
                signals.append(
                    Signal(
                        "revenue_drop_30",
                        Priority.MEDIUM,
                        f"Выручка снизилась на {abs(change):.1f}% за {pair - 1}–{pair} годы.",
                        (prev.id, new.id),
                        value=f"{change:.1f}%",
                    )
                )
    profit = _by_year(_facts(snapshot, FactKind.NET_PROFIT))
    if profit:
        year = max(profit)
        fact = profit[year]
        if isinstance(fact.value, Decimal) and fact.value < 0:
            signals.append(
                Signal(
                    "net_loss",
                    Priority.MEDIUM,
                    f"{year} год завершён с чистым убытком.",
                    (fact.id,),
                    value=str(fact.value),
                )
            )
    return signals


def _external_gaps(
    snapshots: dict[Section, ExternalSnapshot],
) -> tuple[list[str], dict[str, Coverage], bool]:
    missing: list[str] = []
    coverage: dict[str, Coverage] = {}
    for section in Section:
        snapshot = snapshots.get(section)
        label = _SECTION_LABELS[section]
        if snapshot is None:
            coverage[section.value] = Coverage.UNAVAILABLE
            missing.append(f"{label}: раздел не запрашивался.")
            continue
        coverage[section.value] = snapshot.coverage
        missing.extend(f"{label}: {reason}" for reason in snapshot.missing)

    company = snapshots.get(Section.COMPANY)
    company_ok = (
        company is not None
        and company.status is FetchStatus.OK
        and any(f.value is not None for f in _facts(company, FactKind.COMPANY_STATUS))
    )
    bankruptcy = snapshots.get(Section.BANKRUPTCY)
    efrsb_full = bankruptcy is not None and bankruptcy.coverage is Coverage.COMPLETE
    finances = snapshots.get(Section.FINANCES)
    two_years = finances is not None and finances.covered_period is not None
    return missing, coverage, company_ok and efrsb_full and two_years


def _internal_gaps(row: CounterpartyRow | None) -> tuple[list[str], Coverage, bool]:
    if row is None:
        return ["Внутренние данные о долге не переданы."], Coverage.UNAVAILABLE, False
    missing = []
    if row.debt is None or row.overdue_days is None:
        missing.append("Нет суммы долга или просрочки на дату анализа.")
    if row.last_payment_date is None:
        missing.append("Нет подтверждённой даты последнего платежа.")
    if not missing:
        return [], Coverage.COMPLETE, True
    known = row.debt is not None or row.overdue_days is not None
    return missing, Coverage.PARTIAL if known else Coverage.UNAVAILABLE, False


def assess(
    inn: str,
    snapshots: Iterable[ExternalSnapshot],
    row: CounterpartyRow | None = None,
    internal_signals: Sequence[Signal] = (),
) -> Assessment:
    """Apply the external rules and assemble one priority for the INN."""
    by_section: dict[Section, ExternalSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.inn != inn:
            raise ValueError("Snapshot belongs to another INN")
        by_section[snapshot.section] = snapshot

    signals = [
        *_company_signals(by_section.get(Section.COMPANY)),
        *_bankruptcy_signals(by_section.get(Section.BANKRUPTCY)),
        *_finance_signals(by_section.get(Section.FINANCES)),
        *internal_signals,
    ]
    external_missing, coverage, external_complete = _external_gaps(by_section)
    internal_missing, internal_coverage, internal_complete = _internal_gaps(row)
    coverage["internal"] = internal_coverage
    base_complete = external_complete and internal_complete
    return Assessment(
        inn=inn,
        priority=combine(signals, base_complete),
        signals=tuple(signals),
        missing_data=tuple(internal_missing + external_missing),
        coverage=coverage,
        base_complete=base_complete,
    )


def report_order_key(assessment: Assessment, row: CounterpartyRow | None) -> tuple:
    """Sort key for «Приоритеты»: rank, then known debt desc, overdue desc, INN.

    Unknown amounts come after known ones inside the same group.
    """
    debt = row.debt if row is not None else None
    overdue = row.overdue_days if row is not None else None
    return (
        _REPORT_RANK[assessment.priority],
        debt is None,
        -(debt or Decimal(0)),
        overdue is None,
        -(overdue or 0),
        assessment.inn,
    )
