"""S4-06: internal indicators of one counterparty (docs/scoring.md, docs/data-contracts.md §2, §4).

Pure domain code: no network, files or clock. Inputs are the «Контрагенты» row, the
payments of the package with the periods the user vouched for, the debt history and,
optionally, the finances snapshot. Nothing is invented from a gap:

- the payment age is confirmed only by the main file or by a payments export that covers
  every day up to the analysis date; otherwise the export gives just «последний платёж в
  предоставленном периоде»;
- the monthly debt change needs a snapshot exactly one calendar month back (the month's
  last day if that day does not exist); a zero base gives «долг появился», not a ratio;
- a contradiction between the main file and an optional one makes the indicator unknown
  until it is fixed — the same rules the package review (S4-03) reports.

``internal_indicators`` returns the values with their status and the signals they fire;
``scoring.assess(..., indicators=...)`` takes them instead of the row's own payment age.
"""

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.external import ExternalSnapshot, Period
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.scoring import (
    INTERNAL_DEBT,
    INTERNAL_LAST_PAYMENT,
    NO_PAYMENTS_AFTER_DAYS,
    Priority,
    RevenueChange,
    Signal,
    revenue_change,
)

DEBT_DOUBLED_RATIO = Decimal("2")

# Package-review conflict kinds (S4-03 ``PackageReview.conflicts``).
DEBT_HISTORY_CONFLICT = "debt_history"
LAST_PAYMENT_CONFLICT = "last_payment"

# IDs of the optional files in «Основания», cited like the other internal values.
INTERNAL_PAYMENTS = "internal-payments"
INTERNAL_DEBT_HISTORY = "internal-debt-history"

_DAY = timedelta(days=1)


def _fmt(day: date) -> str:
    return day.strftime("%d.%m.%Y")


class PaymentStatus(StrEnum):
    CONFIRMED = "confirmed"  # a trusted last payment date and its age
    NONE_SINCE = "none_since"  # full export, no payments since ``no_payments_since``
    IN_PERIOD = "in_period"  # only the last payment inside a partial export
    CONFLICT = "conflict"  # the files contradict each other
    UNKNOWN = "unknown"


class DebtTrend(StrEnum):
    COMPUTED = "computed"  # ratio of the current debt to a positive base
    APPEARED = "appeared"  # base zero, debt now positive
    NO_DEBT = "no_debt"  # zero then and now
    NO_BASE = "no_base"  # no snapshot on the comparable date
    CONFLICT = "conflict"
    UNKNOWN = "unknown"  # no current debt in the main file


@dataclass(frozen=True, slots=True)
class PaymentRecency:
    status: PaymentStatus
    last_payment: date | None = None
    # CONFIRMED: days since the last payment; NONE_SINCE: days known to have no payment.
    age_days: int | None = None
    source: str | None = None  # evidence ID the value rests on
    no_payments_since: date | None = None  # NONE_SINCE: start of the empty covered span
    period_last: date | None = None  # last payment inside the provided export, if any
    reason: str | None = None  # why the age is not confirmed

    @property
    def silent_days(self) -> int | None:
        """Days without any payment that are known for sure; None if not established."""
        if self.status in (PaymentStatus.CONFIRMED, PaymentStatus.NONE_SINCE):
            return self.age_days
        return None


@dataclass(frozen=True, slots=True)
class DebtDynamics:
    trend: DebtTrend
    current: Decimal | None = None
    current_on: date | None = None
    previous: Decimal | None = None
    previous_on: date | None = None
    ratio: Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InternalIndicators:
    inn: str
    analysis_date: date
    payment: PaymentRecency
    debt: DebtDynamics
    revenue: RevenueChange | None = None

    @property
    def payment_settled(self) -> bool:
        """The base set's «last payment» item: a trusted date or a long enough empty span."""
        if self.payment.status is PaymentStatus.CONFIRMED:
            return True
        silent = self.payment.silent_days
        return silent is not None and silent > NO_PAYMENTS_AFTER_DAYS

    @property
    def signals(self) -> tuple[Signal, ...]:
        return tuple(_payment_signals(self) + _debt_signals(self))

    @property
    def missing(self) -> tuple[str, ...]:
        """Indicator gaps worth showing; the payment one counts against the base set."""
        notes = []
        if not self.payment_settled and self.payment.reason:
            notes.append(self.payment.reason)
        if self.debt.reason:
            notes.append(self.debt.reason)
        return tuple(notes)


def month_before(day: date) -> date:
    """The same day one calendar month earlier, or that month's last day (31.03 → 28.02)."""
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def covered_since(periods: Iterable[Period], analysis_date: date) -> date | None:
    """Start of the unbroken covered span that reaches the analysis date, if there is one.

    Periods of several exports join when they overlap or touch (one ends the day before
    the next starts); a gap of even one day breaks the span.
    """
    start: date | None = None
    end: date | None = None
    reach: date | None = None
    for period in sorted(periods, key=lambda p: p.start):
        if end is not None and period.start <= end + _DAY:
            end = max(end, period.end)
        else:
            start, end = period.start, period.end
        if end >= analysis_date:
            reach = start
    return reach


def _inside(day: date, periods: Sequence[Period]) -> bool:
    return any(p.start <= day <= p.end for p in periods)


def last_payment_contradicts(
    last_payment: date | None, paid_on: Iterable[date], periods: Sequence[Period]
) -> bool:
    """The main file's last payment disagrees with the covered export (S4-03 rule).

    Only the vouched-for periods can contradict it: the date lies inside a period but the
    export has no payment that day, or the export has a later payment inside a period.
    """
    if last_payment is None or not periods:
        return False
    dates = set(paid_on)
    missing = _inside(last_payment, periods) and last_payment not in dates
    later = any(d > last_payment and _inside(d, periods) for d in dates)
    return missing or later


def debt_contradicts(
    row: CounterpartyRow, history: Iterable[DebtSnapshot], analysis_date: date
) -> bool:
    """A history snapshot on the row's cut-off date differs from the row's debt (S4-03)."""
    if row.debt is None:
        return False
    cutoff = row.cutoff_date or analysis_date
    return any(s.inn == row.inn and s.cutoff_date == cutoff and s.debt != row.debt for s in history)


def payment_recency(
    row: CounterpartyRow,
    payments: Iterable[PaymentRow],
    periods: Sequence[Period],
    analysis_date: date,
) -> PaymentRecency:
    paid_on = sorted(p.paid_on for p in payments if p.inn == row.inn and p.paid_on <= analysis_date)
    main = row.last_payment_date
    if main is not None and main > analysis_date:
        return PaymentRecency(
            PaymentStatus.CONFLICT,
            reason="Дата последнего платежа позже даты анализа; давность неизвестна до "
            "исправления.",
        )
    if last_payment_contradicts(main, paid_on, periods):
        return PaymentRecency(
            PaymentStatus.CONFLICT,
            reason="Дата последнего платежа в «Контрагентах» не совпадает с выгрузкой "
            "«Платежи»; давность неизвестна до исправления.",
        )
    in_periods = [d for d in paid_on if _inside(d, periods)]
    period_last = max(in_periods, default=None)
    since = covered_since(periods, analysis_date)
    if since is not None:
        # Without a contradiction the main date is either absent or agrees with the export.
        in_span = [d for d in in_periods if d >= since]
        if in_span:
            last = max(in_span)
            source = INTERNAL_LAST_PAYMENT if main == last else INTERNAL_PAYMENTS
            return PaymentRecency(
                PaymentStatus.CONFIRMED,
                last,
                (analysis_date - last).days,
                source,
                period_last=period_last,
            )
        if main is not None:
            return PaymentRecency(
                PaymentStatus.CONFIRMED,
                main,
                (analysis_date - main).days,
                INTERNAL_LAST_PAYMENT,
                no_payments_since=since,
                period_last=period_last,
            )
        silent = (analysis_date - since).days + 1
        return PaymentRecency(
            PaymentStatus.NONE_SINCE,
            age_days=silent,
            source=INTERNAL_PAYMENTS,
            no_payments_since=since,
            period_last=period_last,
            reason=f"В полной выгрузке «Платежи» нет поступлений с {_fmt(since)} "
            f"({silent} дн.); более ранние платежи неизвестны.",
        )
    if main is not None:
        return PaymentRecency(
            PaymentStatus.CONFIRMED,
            main,
            (analysis_date - main).days,
            INTERNAL_LAST_PAYMENT,
            period_last=period_last,
        )
    if period_last is not None:
        return PaymentRecency(
            PaymentStatus.IN_PERIOD,
            period_last=period_last,
            source=INTERNAL_PAYMENTS,
            reason=f"Последний платёж в предоставленном периоде — {_fmt(period_last)}; "
            "выгрузка не доходит до даты анализа, давность не подтверждена.",
        )
    reason = "Нет подтверждённой даты последнего платежа."
    if periods:
        reason = (
            "Нет даты последнего платежа в «Контрагентах», а выгрузка «Платежи» "
            "не доходит до даты анализа."
        )
    return PaymentRecency(PaymentStatus.UNKNOWN, reason=reason)


def debt_dynamics(
    row: CounterpartyRow, history: Iterable[DebtSnapshot], analysis_date: date
) -> DebtDynamics:
    snapshots = [s for s in history if s.inn == row.inn]
    current_on = row.cutoff_date or analysis_date
    if row.debt is None:
        return DebtDynamics(DebtTrend.UNKNOWN, current_on=current_on)
    if debt_contradicts(row, snapshots, analysis_date):
        return DebtDynamics(
            DebtTrend.CONFLICT,
            row.debt,
            current_on,
            reason="Долг в «Истории долга» на дату среза расходится с «Контрагентами»; "
            "динамика долга неизвестна до исправления.",
        )
    previous_on = month_before(current_on)
    base = next((s.debt for s in snapshots if s.cutoff_date == previous_on), None)
    if base is None:
        reason = None
        # Without any history there is nothing to compare; «О проверке» lists the files.
        if snapshots:
            reason = (
                f"Динамика долга: нет среза на {_fmt(previous_on)} в «Истории долга»; "
                "ближайшая дата не подставляется."
            )
        return DebtDynamics(
            DebtTrend.NO_BASE, row.debt, current_on, previous_on=previous_on, reason=reason
        )
    if base == 0:
        trend = DebtTrend.APPEARED if row.debt > 0 else DebtTrend.NO_DEBT
        return DebtDynamics(trend, row.debt, current_on, base, previous_on)
    ratio = row.debt / base
    return DebtDynamics(DebtTrend.COMPUTED, row.debt, current_on, base, previous_on, ratio)


def _payment_signals(ind: "InternalIndicators") -> list[Signal]:
    payment = ind.payment
    silent = payment.silent_days
    if silent is None or silent <= NO_PAYMENTS_AFTER_DAYS:
        return []
    debt = ind.debt.current
    if debt is None or debt <= 0:
        return []
    if payment.status is PaymentStatus.NONE_SINCE:
        return [
            Signal(
                "no_payments_60",
                Priority.MEDIUM,
                f"В полной выгрузке платежей нет поступлений за последние {silent} дн. "
                "при положительном долге.",
                (INTERNAL_PAYMENTS, INTERNAL_DEBT),
                value=str(silent),
                observed_on=payment.no_payments_since,
            )
        ]
    return [
        Signal(
            "no_payments_60",
            Priority.MEDIUM,
            f"Последний подтверждённый платёж {silent} дн. назад при положительном долге.",
            (payment.source or INTERNAL_LAST_PAYMENT, INTERNAL_DEBT),
            value=str(silent),
            observed_on=payment.last_payment,
        )
    ]


def _debt_signals(ind: "InternalIndicators") -> list[Signal]:
    debt = ind.debt
    if debt.trend is not DebtTrend.COMPUTED or debt.ratio is None:
        return []
    if debt.ratio < DEBT_DOUBLED_RATIO or debt.previous_on is None:
        return []
    return [
        Signal(
            "debt_doubled",
            Priority.MEDIUM,
            f"Долг вырос в {debt.ratio:.2f} раза за месяц (с {_fmt(debt.previous_on)}).",
            (INTERNAL_DEBT, INTERNAL_DEBT_HISTORY),
            value=f"{debt.ratio:.2f}",
            observed_on=debt.current_on,
        )
    ]


def internal_indicators(
    row: CounterpartyRow,
    analysis_date: date,
    payments: Iterable[PaymentRow] = (),
    periods: Sequence[Period] = (),
    history: Iterable[DebtSnapshot] = (),
    finances: ExternalSnapshot | None = None,
) -> InternalIndicators:
    """All internal indicators of one counterparty on the analysis date.

    ``periods`` are the covered periods of the payments files (``UploadedFile.coverage``);
    ``payments`` and ``history`` may hold rows of other INNs, they are filtered here.
    """
    history = tuple(history)
    return InternalIndicators(
        inn=row.inn,
        analysis_date=analysis_date,
        payment=payment_recency(row, payments, tuple(periods), analysis_date),
        debt=debt_dynamics(row, history, analysis_date),
        revenue=revenue_change(finances),
    )


def package_indicators(
    rows: Iterable[CounterpartyRow],
    analysis_date: date,
    payments: Iterable[PaymentRow] = (),
    periods: Sequence[Period] = (),
    history: Iterable[DebtSnapshot] = (),
    finances: Mapping[str, ExternalSnapshot] = MappingProxyType({}),
) -> dict[str, InternalIndicators]:
    """Indicators of every row of a package, grouping the optional files by INN once."""
    paid: dict[str, list[PaymentRow]] = {}
    for payment in payments:
        paid.setdefault(payment.inn, []).append(payment)
    snapshots: dict[str, list[DebtSnapshot]] = {}
    for snapshot in history:
        snapshots.setdefault(snapshot.inn, []).append(snapshot)
    return {
        row.inn: internal_indicators(
            row,
            analysis_date,
            paid.get(row.inn, ()),
            periods,
            snapshots.get(row.inn, ()),
            finances.get(row.inn),
        )
        for row in rows
    }
