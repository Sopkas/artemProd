"""S3-05: external priority rules and the overall priority assembly (docs/scoring.md v0.1).

Pure domain code: no network, files or clock. Inputs are the normalized section snapshots
of one INN and, optionally, its «Контрагенты» row. Missing data is never read as zero: an
indicator that cannot be checked goes to ``missing_data`` and, without any signal, the
result is ``unknown`` («недостаточно данных»), not ``low``.

Internal signals from the «Контрагенты» row (S4-07) are computed here: overdue buckets and
the age of the last payment. Signals that need the payments file or the debt history
(S4-05) arrive through ``internal_signals``; the assembly treats every signal alike.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from claims_assistant.domain.indicators import InternalIndicators

RULES_VERSION = "0.1"
REVENUE_DROP_PERCENT = Decimal("30")
# A net profit that fell 1.5 times or more and stayed a profit: as alarming as revenue down
# 30 % (the customer, 17.10.2026 — question 7 of S8-07). Reaching it fires, like revenue.
PROFIT_DROP_TIMES = Decimal("1.5")
# Thresholds are exclusive lower bounds: 60 days is still medium, 61 is high.
OVERDUE_HIGH_AFTER_DAYS = 60
OVERDUE_MEDIUM_AFTER_DAYS = 30
NO_PAYMENTS_AFTER_DAYS = 60

# IDs of the internal values in «Основания»; signals cite them like external fact IDs.
INTERNAL_DEBT = "internal-debt"
INTERNAL_OVERDUE = "internal-overdue"
INTERNAL_LAST_PAYMENT = "internal-last-payment"


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
    Section.COURTS: "Арбитражные дела",
    Section.FEDRESURS: "Сообщения Федресурса",
    Section.ENFORCEMENTS: "Исполнительные производства",
}
# S8-01: a creditor's notice of intention is followed by the filing within weeks; a notice
# older than a year is history, shown among the grounds but raising nothing.
INTENT_WINDOW_DAYS = 365


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
    coverage: Mapping[str, Coverage]
    base_complete: bool
    rules_version: str = RULES_VERSION

    def __post_init__(self) -> None:
        # A read-only copy: the assessment is a snapshot and must not change afterwards.
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))

    def __hash__(self) -> int:
        return hash(
            (
                self.inn,
                self.priority,
                self.signals,
                self.missing_data,
                tuple(sorted(self.coverage.items())),
                self.base_complete,
                self.rules_version,
            )
        )

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
        if codes & {"revenue_drop_30", "net_loss", "net_profit_drop"}:
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


def _recent(facts: list[Fact], today: date | None) -> list[Fact]:
    """A Fedresurs notice older than a year is history (``INTENT_WINDOW_DAYS``)."""
    if today is None:
        return facts
    since = today - timedelta(days=INTENT_WINDOW_DAYS)
    return [f for f in facts if f.observed_on is not None and since <= f.observed_on]


def _filing_signals(
    courts: ExternalSnapshot | None, fedresurs: ExternalSnapshot | None, today: date | None
) -> list[Signal]:
    """S8-01: an application to declare the company bankrupt is in court. Two sources say
    it — an open arbitration case of the bankruptcy kind with the company as the debtor
    (whoever filed), and the tax authority's notice on Fedresurs that it has filed — and
    they are one event, so one signal with both as grounds. The customer's debtor of
    15.10.2026 had both and nothing in EFRSB. High, not critical: a court may yet reject
    the application, and a procedure is confirmed by the register or the company's status.
    """
    cases = _facts(courts, FactKind.BANKRUPTCY_CASE)  # open ones only: the request says so
    notices = _recent(_facts(fedresurs, FactKind.BANKRUPTCY_FILING), today)
    grounds = cases + notices
    if not grounds:
        return []
    filed = max((f.observed_on for f in grounds if f.observed_on), default=None)
    when = f" (заявление от {filed:%d.%m.%Y})" if filed else ""
    return [
        Signal(
            "bankruptcy_filed",
            Priority.HIGH,
            f"В арбитражном суде заявление о признании организации банкротом{when}.",
            tuple(f.id for f in grounds),
            value=str(len(cases) or len(notices)),
            observed_on=filed,
        )
    ]


def _intent_signals(snapshot: ExternalSnapshot | None, today: date | None) -> list[Signal]:
    """S8-01: a creditor announced on Fedresurs that it is going to court to bankrupt the
    company. The register (EFRSB) stays silent until a court opens the case, so this is the
    earliest public sign."""
    notices = _recent(_facts(snapshot, FactKind.BANKRUPTCY_INTENT), today)
    if not notices:
        return []
    latest = max((f.observed_on for f in notices if f.observed_on), default=None)
    when = f" (последнее — {latest:%d.%m.%Y})" if latest else ""
    return [
        Signal(
            "bankruptcy_intent",
            Priority.HIGH,
            f"Кредитор объявил на Федресурсе о намерении подать заявление о банкротстве{when}.",
            tuple(f.id for f in notices),
            value=str(len(notices)),
            observed_on=latest,
        )
    ]


def _by_year(facts: Iterable[Fact]) -> dict[int, Fact]:
    """Known values of full calendar years only: a quarter is not compared with a year."""
    return {
        f.period.end.year: f
        for f in facts
        if f.period is not None
        and isinstance(f.value, Decimal)
        and f.period.start == date(f.period.end.year, 1, 1)
        and f.period.end == date(f.period.end.year, 12, 31)
    }


def _two_consecutive_years(snapshot: ExternalSnapshot | None) -> bool:
    """The base-set condition: one indicator known for two consecutive comparable years."""
    for kind in (FactKind.REVENUE, FactKind.NET_PROFIT):
        years = _by_year(_facts(snapshot, kind))
        if any(y - 1 in years and years[y - 1].unit == years[y].unit for y in years):
            return True
    return False


@dataclass(frozen=True, slots=True)
class RevenueChange:
    """Revenue change between the two latest consecutive full years (S4-06 indicator)."""

    year: int
    percent: Decimal
    fact_ids: tuple[str, str]  # previous year, new year


def revenue_change(snapshot: ExternalSnapshot | None) -> RevenueChange | None:
    """Only a positive base and matching units give a percent; otherwise there is no trend."""
    revenue = _by_year(_facts(snapshot, FactKind.REVENUE))
    pair = next((y for y in sorted(revenue, reverse=True) if y - 1 in revenue), None)
    if pair is None:
        return None
    new, prev = revenue[pair], revenue[pair - 1]
    if not (
        isinstance(new.value, Decimal)
        and isinstance(prev.value, Decimal)
        and prev.value > 0
        and new.unit == prev.unit
    ):
        return None
    percent = (new.value - prev.value) / prev.value * 100
    return RevenueChange(pair, percent, (prev.id, new.id))


def _finance_signals(snapshot: ExternalSnapshot | None) -> list[Signal]:
    signals = []
    change = revenue_change(snapshot)
    if change is not None and change.percent <= -REVENUE_DROP_PERCENT:
        signals.append(
            Signal(
                "revenue_drop_30",
                Priority.MEDIUM,
                # A person reads it: «35,5%»; ``value`` stays machine-read.
                f"Выручка снизилась на {f'{abs(change.percent):.1f}'.replace('.', ',')}% "
                f"за {change.year - 1}–{change.year} годы.",
                change.fact_ids,
                value=f"{change.percent:.1f}%",
            )
        )
    profit = _by_year(_facts(snapshot, FactKind.NET_PROFIT))
    drop = _profit_drop(profit)
    if drop is not None:
        signals.append(drop)
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


def _profit_drop(profit: dict[int, Fact]) -> Signal | None:
    """The two latest consecutive full years, as for revenue: a profit both years, the new
    one at most the old one divided by 1.5. A loss is ``net_loss``, never both."""
    year = next((y for y in sorted(profit, reverse=True) if y - 1 in profit), None)
    if year is None:
        return None
    new, prev = profit[year], profit[year - 1]
    if not (
        isinstance(new.value, Decimal)
        and isinstance(prev.value, Decimal)
        and new.unit == prev.unit
        and prev.value > 0
        and new.value >= 0
        and new.value * PROFIT_DROP_TIMES <= prev.value
    ):
        return None
    percent = (new.value - prev.value) / prev.value * 100
    # «В 5,0 раза»: the threshold is in times, so the reason says it the same way. With a
    # fractional figure the word is always «раза».
    how = (
        "до нуля"
        if new.value == 0
        else f"в {f'{prev.value / new.value:.1f}'.replace('.', ',')} раза "
        f"(на {f'{abs(percent):.1f}'.replace('.', ',')}%)"
    )
    return Signal(
        "net_profit_drop",
        Priority.MEDIUM,
        f"Чистая прибыль снизилась {how} за {year - 1}–{year} годы.",
        (prev.id, new.id),
        value=f"{percent:.1f}%",
    )


class Trend(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


# Less than half a percent either way reads as «без изменений» in whole percents.
FLAT_BELOW_PERCENT = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class FinanceTrend:
    """S8-04: one indicator year over year, for a mark beside its figure in the card.

    ``alarm`` is the code of the rule of this module that fires on it, and only then: a
    mark never says more than the priority does. A profit falling by 80 % without a loss
    had a downward trend and no alarm until the customer named the threshold: since
    17.10.2026 a fall of 1.5 times or more is ``net_profit_drop`` (docs/scoring.md).
    """

    kind: FactKind
    year: int
    previous: Decimal
    value: Decimal
    unit: str | None
    # None when the previous year was not positive: «−150 %» of a loss means nothing.
    percent: Decimal | None
    trend: Trend
    alarm: str | None
    fact_ids: tuple[str, str]  # previous year, new year


def _trend(kind: FactKind, years: dict[int, Fact]) -> FinanceTrend | None:
    """The two latest consecutive full years, as ``revenue_change`` picks them."""
    year = next((y for y in sorted(years, reverse=True) if y - 1 in years), None)
    if year is None:
        return None
    new, prev = years[year], years[year - 1]
    if not (isinstance(new.value, Decimal) and isinstance(prev.value, Decimal)):
        return None
    if new.unit != prev.unit:
        return None
    percent = None
    if prev.value > 0:
        percent = (new.value - prev.value) / prev.value * 100
        flat = abs(percent) < FLAT_BELOW_PERCENT
    else:
        flat = new.value == prev.value
    if flat:
        trend = Trend.FLAT
    else:
        trend = Trend.UP if new.value > prev.value else Trend.DOWN
    return FinanceTrend(
        kind, year, prev.value, new.value, new.unit, percent, trend, None, (prev.id, new.id)
    )


def finance_trends(snapshot: ExternalSnapshot | None) -> tuple[FinanceTrend, ...]:
    """Revenue and net profit year over year; an indicator without two years is left out.

    The alarm is taken from the finance rules themselves, matched by the new year's fact,
    so a changed threshold changes the mark and the priority together.
    """
    fired = {
        (signal.code, fact_id)
        for signal in _finance_signals(snapshot)
        for fact_id in signal.fact_ids
    }
    trends = []
    rules = (
        (FactKind.REVENUE, ("revenue_drop_30",)),
        (FactKind.NET_PROFIT, ("net_loss", "net_profit_drop")),
    )
    for kind, codes in rules:
        trend = _trend(kind, _by_year(_facts(snapshot, kind)))
        if trend is None:
            continue
        code = next((c for c in codes if (c, trend.fact_ids[1]) in fired), None)
        if code is not None:
            trend = replace(trend, alarm=code)
        trends.append(trend)
    return tuple(trends)


def payment_age_days(row: CounterpartyRow, analysis_date: date) -> int | None:
    """Days from the last confirmed payment to the analysis date; None if unknown.

    A payment dated after the analysis date is a contradiction, not an age of zero.
    """
    if row.last_payment_date is None:
        return None
    age = (analysis_date - row.last_payment_date).days
    return age if age >= 0 else None


def row_signals(
    row: CounterpartyRow, analysis_date: date | None, *, payment_age: bool = True
) -> tuple[Signal, ...]:
    """Overdue buckets and payment age from the row; unknown values never fire a rule.

    ``payment_age=False`` leaves the payment age to the indicators (S4-06), which also
    check the date against the payments export.
    """
    signals = []
    days = row.overdue_days
    # The overdue rules are mutually exclusive.
    if days is not None and days > OVERDUE_HIGH_AFTER_DAYS:
        signals.append(
            Signal(
                "overdue_60",
                Priority.HIGH,
                f"Просрочка {days} дн. — больше {OVERDUE_HIGH_AFTER_DAYS}.",
                (INTERNAL_OVERDUE,),
                value=str(days),
                observed_on=row.cutoff_date,
            )
        )
    elif days is not None and days > OVERDUE_MEDIUM_AFTER_DAYS:
        signals.append(
            Signal(
                "overdue_30",
                Priority.MEDIUM,
                f"Просрочка {days} дн. — от {OVERDUE_MEDIUM_AFTER_DAYS + 1} "
                f"до {OVERDUE_HIGH_AFTER_DAYS}.",
                (INTERNAL_OVERDUE,),
                value=str(days),
                observed_on=row.cutoff_date,
            )
        )
    known_date = analysis_date is not None and payment_age
    age = payment_age_days(row, analysis_date) if known_date else None
    if age is not None and age > NO_PAYMENTS_AFTER_DAYS and row.debt is not None and row.debt > 0:
        signals.append(
            Signal(
                "no_payments_60",
                Priority.MEDIUM,
                f"Последний подтверждённый платёж {age} дн. назад при положительном долге.",
                (INTERNAL_LAST_PAYMENT, INTERNAL_DEBT),
                value=str(age),
                observed_on=row.last_payment_date,
            )
        )
    return tuple(signals)


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
    # Checked on the facts, like the finance signals, not on the declared covered period.
    finances = snapshots.get(Section.FINANCES)
    two_years = _two_consecutive_years(finances)
    answered = finances is not None and finances.status is FetchStatus.OK
    if answered and not two_years and not finances.missing:
        # The section named no gap of its own, yet the base set is not met: without this
        # line the report says «неполная» and nothing says why (review A on #61).
        missing.append(
            f"{_SECTION_LABELS[Section.FINANCES]}: нет показателя за два года подряд; "
            "для полной оценки нужны оба."
        )
    return missing, coverage, company_ok and efrsb_full and two_years


def _internal_gaps(
    row: CounterpartyRow | None, indicators: "InternalIndicators | None" = None
) -> tuple[list[str], Coverage, bool]:
    if row is None:
        return ["Внутренние данные о долге не переданы."], Coverage.UNAVAILABLE, False
    missing = []
    if row.debt is None or row.overdue_days is None:
        missing.append("Нет суммы долга или просрочки на дату анализа.")
    if indicators is None:
        if row.last_payment_date is None:
            missing.append("Нет подтверждённой даты последнего платежа.")
        payment_settled = row.last_payment_date is not None
    else:
        payment_settled = indicators.payment_settled
    # Indicator notes (payment age, debt dynamics); only the payment one blocks the base set.
    notes = list(indicators.missing) if indicators is not None else []
    if not missing and payment_settled:
        return notes, Coverage.COMPLETE, True
    known = row.debt is not None or row.overdue_days is not None
    return missing + notes, Coverage.PARTIAL if known else Coverage.UNAVAILABLE, False


def assess(
    inn: str,
    snapshots: Iterable[ExternalSnapshot],
    row: CounterpartyRow | None = None,
    internal_signals: Sequence[Signal] = (),
    analysis_date: date | None = None,
    indicators: "InternalIndicators | None" = None,
) -> Assessment:
    """Apply the external and internal rules and assemble one priority for the INN.

    Internal signals are computed from ``row``; the analysis date defaults to the row's
    cut-off date, which the parser keeps equal to it. ``indicators`` (S4-06) replace the
    row's own payment age with the one checked against the payments export and add the
    debt dynamics; ``internal_signals`` adds signals from any other data.
    """
    if indicators is not None and indicators.inn != inn:
        raise ValueError("Indicators belong to another INN")
    by_section: dict[Section, ExternalSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.inn != inn:
            raise ValueError("Snapshot belongs to another INN")
        by_section[snapshot.section] = snapshot
    # The card has no analysis date: the day the source answered stands for «today».
    today = (
        analysis_date
        or (row.cutoff_date if row is not None else None)
        or _fetched_on(by_section.get(Section.FEDRESURS))
    )

    signals = [
        *_company_signals(by_section.get(Section.COMPANY)),
        # A filing in court comes first among the reasons, and once it is there the creditors'
        # earlier intentions say nothing more: one reason, not two (review A on #89).
        *(
            filed := _filing_signals(
                by_section.get(Section.COURTS), by_section.get(Section.FEDRESURS), today
            )
        ),
        *_bankruptcy_signals(by_section.get(Section.BANKRUPTCY)),
        *([] if filed else _intent_signals(by_section.get(Section.FEDRESURS), today)),
        *_finance_signals(by_section.get(Section.FINANCES)),
        *(
            row_signals(row, analysis_date or row.cutoff_date, payment_age=indicators is None)
            if row is not None
            else ()
        ),
        *(indicators.signals if indicators is not None else ()),
        *internal_signals,
    ]
    external_missing, coverage, external_complete = _external_gaps(by_section)
    internal_missing, internal_coverage, internal_complete = _internal_gaps(row, indicators)
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


def _fetched_on(snapshot: ExternalSnapshot | None) -> date | None:
    """The card has no analysis date: the day the source answered stands for «today»."""
    return snapshot.fetched_at.date() if snapshot is not None else None


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
