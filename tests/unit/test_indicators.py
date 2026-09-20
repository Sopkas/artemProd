"""S4-06: internal indicators — payment age, monthly debt change, revenue change.

The roadmap criterion: a zero base, an incomplete period and contradictions never give an
invented indicator. Synthetic rows only.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.external import Period
from claims_assistant.domain.indicators import (
    INTERNAL_DEBT_HISTORY,
    INTERNAL_PAYMENTS,
    DebtTrend,
    PaymentStatus,
    covered_since,
    debt_dynamics,
    internal_indicators,
    month_before,
    package_indicators,
    payment_recency,
)
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.scoring import INTERNAL_LAST_PAYMENT, Priority, assess
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import normalize_company

INN = "1234567894"
OTHER = "7707083893"
DAY = date(2026, 9, 1)  # analysis date
DEBT = Decimal("1000.00")
NOW = datetime(2026, 9, 18, tzinfo=UTC)


def row(last=None, debt=DEBT, cutoff=DAY, inn=INN, overdue=0):
    return CounterpartyRow(
        inn=inn, cutoff_date=cutoff, debt=debt, overdue_days=overdue, last_payment_date=last
    )


def pay(day, pid="1", inn=INN):
    return PaymentRow(inn=inn, payment_id=pid, paid_on=day, amount=Decimal("10.00"))


def days_ago(n):
    return DAY - timedelta(days=n)


def period(start_days_ago, end=DAY):
    return Period(days_ago(start_days_ago), end)


def snap(day, debt, inn=INN):
    return DebtSnapshot(inn=inn, cutoff_date=day, debt=Decimal(debt))


def clean_external(years=None):
    """Company, EFRSB and finances that complete the external part of the base set."""
    company = normalize_company(
        {
            "meta": {"status": "ok"},
            "data": {
                "ИНН": INN,
                "ОГРН": "0000000000000",
                "НаимСокр": "ДЕМО",
                "ДатаВып": "2026-09-01",
                "Статус": {"Код": "001", "Наим": "Действует"},
            },
        },
        INN,
        NOW,
    )
    efrsb = bankruptcy.project_bankruptcy(INN, [], NOW, complete=True, unreadable=0)
    fin = finances.project_finances(
        INN, years or {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 1100, "2400": 20}}, NOW
    )
    return [company, efrsb, fin]


# --- calendar helpers ---


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 3, 31), date(2026, 2, 28)),
        (date(2024, 3, 31), date(2024, 2, 29)),  # leap year
        (date(2026, 5, 31), date(2026, 4, 30)),
        (date(2026, 4, 30), date(2026, 3, 30)),  # the same day, not the month's end
        (date(2026, 1, 15), date(2025, 12, 15)),
    ],
)
def test_month_before_takes_the_same_day_or_the_months_last_day(day, expected):
    assert month_before(day) == expected


def test_covered_since_joins_touching_periods_and_breaks_on_a_gap():
    touching = [Period(date(2026, 7, 1), date(2026, 7, 31)), Period(date(2026, 8, 1), DAY)]
    assert covered_since(touching, DAY) == date(2026, 7, 1)
    overlapping = [Period(date(2026, 6, 1), date(2026, 8, 10)), Period(date(2026, 8, 1), DAY)]
    assert covered_since(overlapping, DAY) == date(2026, 6, 1)
    gap = [Period(date(2026, 7, 1), date(2026, 7, 30)), Period(date(2026, 8, 1), DAY)]
    assert covered_since(gap, DAY) == date(2026, 8, 1)
    assert covered_since([Period(date(2026, 7, 1), date(2026, 8, 31))], DAY) is None
    assert covered_since([], DAY) is None


# --- payment age ---


def test_main_file_date_alone_is_confirmed():
    recency = payment_recency(row(last=days_ago(10)), [], (), DAY)
    assert recency.status is PaymentStatus.CONFIRMED
    assert (recency.last_payment, recency.age_days) == (days_ago(10), 10)
    assert recency.source == INTERNAL_LAST_PAYMENT


def test_full_export_confirms_its_latest_payment():
    payments = [pay(days_ago(40), "1"), pay(days_ago(5), "2"), pay(days_ago(1), "3", OTHER)]
    recency = payment_recency(row(), payments, (period(90),), DAY)
    assert recency.status is PaymentStatus.CONFIRMED
    assert (recency.last_payment, recency.age_days) == (days_ago(5), 5)
    assert recency.source == INTERNAL_PAYMENTS
    same = payment_recency(row(last=days_ago(5)), payments, (period(90),), DAY)
    assert same.source == INTERNAL_LAST_PAYMENT  # both files agree


def test_full_export_without_payments_confirms_their_absence():
    recency = payment_recency(row(), [], (period(60),), DAY)
    assert recency.status is PaymentStatus.NONE_SINCE
    assert recency.no_payments_since == days_ago(60)
    assert recency.silent_days == 61  # the covered days, both ends included
    # A main-file date before the span stays the confirmed last payment.
    earlier = payment_recency(row(last=days_ago(100)), [], (period(60),), DAY)
    assert earlier.status is PaymentStatus.CONFIRMED and earlier.age_days == 100
    assert earlier.no_payments_since == days_ago(60)


def test_a_gap_between_exports_is_named_in_the_reason():
    # 30.06 and 02.07 typed by hand: 01.07 is not covered.
    periods = (Period(date(2026, 6, 1), date(2026, 6, 30)), Period(date(2026, 7, 2), DAY))
    recency = payment_recency(row(), [pay(date(2026, 6, 15))], periods, DAY)
    assert recency.status is PaymentStatus.NONE_SINCE
    assert recency.no_payments_since == date(2026, 7, 2)
    assert recency.gap == Period(date(2026, 7, 1), date(2026, 7, 1))
    assert recency.period_last == date(2026, 6, 15)
    assert "разрыв 01.07.2026–01.07.2026" in recency.reason
    assert "15.06.2026" in recency.reason
    touching = (Period(date(2026, 6, 1), date(2026, 6, 30)), Period(date(2026, 7, 1), DAY))
    joined = payment_recency(row(), [pay(date(2026, 6, 15))], touching, DAY)
    assert joined.status is PaymentStatus.CONFIRMED and joined.gap is None


def test_partial_export_gives_only_the_last_payment_in_the_period():
    partial = (Period(days_ago(90), days_ago(20)),)
    recency = payment_recency(row(), [pay(days_ago(30))], partial, DAY)
    assert recency.status is PaymentStatus.IN_PERIOD
    assert recency.period_last == days_ago(30)
    assert recency.age_days is None and recency.silent_days is None
    assert "в предоставленном периоде" in recency.reason
    # An empty partial export proves nothing either (contract §2).
    empty = payment_recency(row(), [], partial, DAY)
    assert empty.status is PaymentStatus.UNKNOWN and empty.silent_days is None


@pytest.mark.parametrize(
    "last,payments,periods",
    [
        (days_ago(90), [pay(days_ago(10))], (period(120),)),  # later payment in the export
        (days_ago(20), [pay(days_ago(30))], (period(120),)),  # main date not in the export
        (days_ago(20), [], (period(60),)),  # main date inside an empty full export
        (DAY + timedelta(days=1), [], ()),  # after the analysis date
    ],
)
def test_contradictions_make_the_age_unknown_until_fixed(last, payments, periods):
    recency = payment_recency(row(last=last), payments, periods, DAY)
    assert recency.status is PaymentStatus.CONFLICT
    assert recency.age_days is None and recency.silent_days is None
    assert "до исправления" in recency.reason


def test_no_payment_data_at_all_is_unknown():
    recency = payment_recency(row(), [], (), DAY)
    assert recency.status is PaymentStatus.UNKNOWN
    assert recency.reason == "Нет подтверждённой даты последнего платежа."


# --- monthly debt change ---


def test_debt_ratio_against_the_same_day_a_month_back():
    dynamics = debt_dynamics(row(debt=Decimal("2500")), [snap(date(2026, 8, 1), "1000")], DAY)
    assert dynamics.trend is DebtTrend.COMPUTED
    assert dynamics.ratio == Decimal("2.5")
    assert (dynamics.previous_on, dynamics.current_on) == (date(2026, 8, 1), DAY)


def test_month_end_cutoff_compares_with_the_previous_months_last_day():
    march = date(2026, 3, 31)
    dynamics = debt_dynamics(
        row(debt=Decimal("300"), cutoff=march), [snap(date(2026, 2, 28), "100")], march
    )
    assert dynamics.previous_on == date(2026, 2, 28) and dynamics.ratio == 3


def test_zero_base_is_debt_appeared_without_a_ratio():
    appeared = debt_dynamics(row(debt=Decimal("500")), [snap(date(2026, 8, 1), "0")], DAY)
    assert appeared.trend is DebtTrend.APPEARED and appeared.ratio is None
    none = debt_dynamics(row(debt=Decimal("0")), [snap(date(2026, 8, 1), "0")], DAY)
    assert none.trend is DebtTrend.NO_DEBT and none.ratio is None
    indicators = internal_indicators(
        row(debt=Decimal("500")), DAY, history=[snap(date(2026, 8, 1), "0")]
    )
    assert "debt_doubled" not in {s.code for s in indicators.signals}


def test_no_snapshot_on_the_exact_date_is_not_replaced_by_a_nearby_one():
    dynamics = debt_dynamics(row(), [snap(date(2026, 8, 2), "100")], DAY)
    assert dynamics.trend is DebtTrend.NO_BASE and dynamics.ratio is None
    assert "01.08.2026" in dynamics.reason
    # Without any history the gap is the missing file, not a per-company note.
    assert debt_dynamics(row(), [], DAY).reason is None


def test_debt_conflict_with_the_main_file_is_unknown():
    history = [snap(DAY, "999"), snap(date(2026, 8, 1), "100")]
    dynamics = debt_dynamics(row(), history, DAY)
    assert dynamics.trend is DebtTrend.CONFLICT and dynamics.ratio is None
    assert "до исправления" in dynamics.reason
    agreeing = debt_dynamics(row(), [snap(DAY, "1000.00"), snap(date(2026, 8, 1), "100")], DAY)
    assert agreeing.trend is DebtTrend.COMPUTED


def test_unknown_current_debt_gives_no_dynamics():
    dynamics = debt_dynamics(row(debt=None), [snap(date(2026, 8, 1), "100")], DAY)
    assert dynamics.trend is DebtTrend.UNKNOWN and dynamics.ratio is None


@pytest.mark.parametrize("current,fires", [("2000", True), ("1999.99", False)])
def test_debt_doubled_fires_from_exactly_twice(current, fires):
    indicators = internal_indicators(
        row(debt=Decimal(current)), DAY, history=[snap(date(2026, 8, 1), "1000")]
    )
    signals = [s for s in indicators.signals if s.code == "debt_doubled"]
    assert bool(signals) is fires
    if fires:
        assert signals[0].level is Priority.MEDIUM
        assert signals[0].fact_ids == ("internal-debt", INTERNAL_DEBT_HISTORY)


# --- signals and the assessment ---


@pytest.mark.parametrize("span_days,fires", [(60, True), (59, False)])
def test_empty_full_export_fires_no_payments_after_sixty_days(span_days, fires):
    # period(60) covers 61 days: more than 60 without payments.
    indicators = internal_indicators(row(), DAY, periods=(period(span_days),))
    assert indicators.payment_settled is fires
    assert ("no_payments_60" in {s.code for s in indicators.signals}) is fires
    if not fires:
        assert any("более ранние платежи неизвестны" in note for note in indicators.missing)


def test_no_payments_needs_a_positive_debt():
    indicators = internal_indicators(row(debt=Decimal("0")), DAY, periods=(period(90),))
    assert indicators.signals == ()


def test_revenue_change_is_available_only_with_a_positive_base():
    company, efrsb, fin = clean_external({2024: {"2110": 1000}, 2025: {"2110": 600}})
    change = internal_indicators(row(), DAY, finances=fin).revenue
    assert change.year == 2025 and change.percent == Decimal("-40")
    _, _, zero = clean_external({2024: {"2110": 0}, 2025: {"2110": 600}})
    assert internal_indicators(row(), DAY, finances=zero).revenue is None


def test_assess_takes_the_payment_age_from_the_indicators():
    old = row(last=days_ago(90))
    # Without indicators the row alone fires the rule.
    assert "no_payments_60" in {s.code for s in assess(INN, clean_external(), old).signals}
    # The export contradicts the date: the age is unknown, the rule does not fire.
    indicators = internal_indicators(old, DAY, [pay(days_ago(10))], (period(120),))
    assessment = assess(INN, clean_external(), old, indicators=indicators)
    assert "no_payments_60" not in {s.code for s in assessment.signals}
    assert assessment.priority is Priority.UNKNOWN and not assessment.base_complete
    assert any("до исправления" in reason for reason in assessment.missing_data)


def test_assess_counts_a_confirmed_absence_in_the_base_set():
    indicators = internal_indicators(
        row(), DAY, periods=(period(90),), history=[snap(date(2026, 8, 1), "400")]
    )
    assessment = assess(INN, clean_external(), row(), indicators=indicators)
    codes = {s.code for s in assessment.signals}
    assert codes == {"no_payments_60", "debt_doubled"}
    assert assessment.priority is Priority.HIGH  # two different medium signals
    assert assessment.base_complete


def test_assess_rejects_indicators_of_another_company():
    indicators = internal_indicators(row(inn=OTHER), DAY)
    with pytest.raises(ValueError):
        assess(INN, clean_external(), row(), indicators=indicators)


def test_package_indicators_keep_each_company_to_its_own_rows():
    rows = [row(), row(inn=OTHER, last=days_ago(3))]
    result = package_indicators(
        rows,
        DAY,
        payments=[pay(days_ago(5)), pay(days_ago(3), inn=OTHER)],
        periods=(period(30),),
        history=[snap(date(2026, 8, 1), "250", inn=OTHER)],
        finances={INN: clean_external()[2]},
    )
    assert result[INN].payment.last_payment == days_ago(5)
    assert result[OTHER].payment.source == INTERNAL_LAST_PAYMENT
    assert result[OTHER].debt.ratio == 4 and result[INN].debt.trend is DebtTrend.NO_BASE
    assert result[INN].revenue is not None and result[OTHER].revenue is None
