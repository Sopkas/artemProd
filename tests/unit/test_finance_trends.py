"""S8-04 (part B): revenue and profit year over year, and when the mark is an alarm.

The customer asked for a mark beside each figure — «выручка снизилась → красный
восклицательный знак». The alarm comes from the rules, so the card's marks and its
priority never disagree. Snapshots are built by the finances adapter from line codes
(2110 revenue, 2400 net profit).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from claims_assistant.domain.external import FactKind
from claims_assistant.domain.scoring import (
    REVENUE_DROP_PERCENT,
    Trend,
    assess,
    finance_trends,
)
from claims_assistant.infrastructure.checko import finances

INN = "1234567894"
NOW = datetime(2026, 10, 16, tzinfo=UTC)


def fin(years):
    return finances.project_finances(INN, years, NOW)


def trends(years):
    return {trend.kind: trend for trend in finance_trends(fin(years))}


def test_the_customers_example_profit_down_80_without_a_loss_is_the_rules_alarm():
    """15.10.2026: revenue +12 %, net profit 3,8 → 0,76 million. A fall only until the
    customer named the threshold (17.10.2026): 1.5 times or more is ``net_profit_drop``."""
    result = trends(
        {
            2024: {"2110": 100_000_000, "2400": 3_800_000},
            2025: {"2110": 112_000_000, "2400": 760_000},
        }
    )
    revenue, profit = result[FactKind.REVENUE], result[FactKind.NET_PROFIT]
    assert (revenue.trend, revenue.percent, revenue.alarm) == (Trend.UP, Decimal(12), None)
    assert (profit.trend, profit.percent, profit.alarm) == (
        Trend.DOWN,
        Decimal(-80),
        "net_profit_drop",
    )
    assert (profit.year, profit.previous, profit.value) == (2025, Decimal(3_800_000), 760_000)


def test_a_revenue_drop_from_the_threshold_is_the_rules_alarm():
    revenue = trends({2024: {"2110": 1000}, 2025: {"2110": 700}})[FactKind.REVENUE]
    assert revenue.percent == -REVENUE_DROP_PERCENT
    assert (revenue.trend, revenue.alarm) == (Trend.DOWN, "revenue_drop_30")


def test_a_revenue_drop_below_the_threshold_is_a_fall_only():
    revenue = trends({2024: {"2110": 1000}, 2025: {"2110": 701}})[FactKind.REVENUE]
    assert (revenue.trend, revenue.alarm) == (Trend.DOWN, None)


def test_a_profit_fall_short_of_one_and_a_half_times_is_a_fall_only():
    profit = trends({2024: {"2400": 1500}, 2025: {"2400": 1001}})[FactKind.NET_PROFIT]
    assert (profit.trend, profit.alarm) == (Trend.DOWN, None)


def test_a_loss_is_the_rules_alarm_with_its_percent():
    profit = trends({2024: {"2400": 100}, 2025: {"2400": -50}})[FactKind.NET_PROFIT]
    assert (profit.trend, profit.percent, profit.alarm) == (Trend.DOWN, Decimal(-150), "net_loss")


def test_after_a_loss_year_there_is_a_direction_but_no_percent():
    profit = trends({2024: {"2400": -100}, 2025: {"2400": 20}})[FactKind.NET_PROFIT]
    assert (profit.trend, profit.percent, profit.alarm) == (Trend.UP, None, None)


def test_a_smaller_loss_is_still_an_alarm():
    profit = trends({2024: {"2400": -100}, 2025: {"2400": -20}})[FactKind.NET_PROFIT]
    assert (profit.trend, profit.percent, profit.alarm) == (Trend.UP, None, "net_loss")


@pytest.mark.parametrize(
    ("previous", "value", "expected"),
    [(1000, 1004, Trend.FLAT), (1000, 1005, Trend.UP), (1000, 995, Trend.DOWN), (0, 0, Trend.FLAT)],
)
def test_less_than_half_a_percent_is_flat(previous, value, expected):
    revenue = trends({2024: {"2110": previous}, 2025: {"2110": value}})[FactKind.REVENUE]
    assert revenue.trend is expected


@pytest.mark.parametrize(
    "years",
    [
        {2025: {"2110": 100, "2400": 5}},  # one year
        {2022: {"2110": 1000}, 2025: {"2110": 100}},  # not consecutive
    ],
)
def test_without_two_consecutive_years_there_is_no_trend(years):
    assert finance_trends(fin(years)) == ()


def test_no_finances_no_trends():
    assert finance_trends(None) == ()


@pytest.mark.parametrize(
    "years",
    [
        {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 600, "2400": -1}},
        {2024: {"2110": 1000, "2400": -10}, 2025: {"2110": 1200, "2400": -1}},
        {2024: {"2110": 0, "2400": 0}, 2025: {"2110": 100, "2400": 5}},
        {2023: {"2110": 1000}, 2024: {"2110": 500}, 2025: {"2110": 450}},
        {2024: {"2110": 10, "2400": -1}, 2025: {"2110": 10, "2400": 5}},
        {2024: {"2110": 1000, "2400": 30}, 2025: {"2110": 700, "2400": 10}},
        {2024: {"2400": 1500}, 2025: {"2400": 1000}},
        {2024: {"2400": 1000}, 2025: {"2400": 0}},
    ],
)
def test_marks_never_disagree_with_the_priority(years):
    """Every alarm is a finance signal of the assessment, and every finance signal of a
    year with a trend is an alarm."""
    snapshot = fin(years)
    alarms = {t.alarm for t in finance_trends(snapshot) if t.alarm}
    signals = {s.code for s in assess(INN, [snapshot]).signals}
    finance_codes = signals & {"revenue_drop_30", "net_loss", "net_profit_drop"}
    assert alarms == finance_codes
