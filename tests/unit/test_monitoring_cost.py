"""S8-12 (part B): the price of a monitored list by how often it is checked.

The customer, 17.10.2026: a pause «чтобы не тратить деньги». The figures are the ones he
was told for Checko's «Стандарт» (S8-07): 100 requests a day free, then 0,15 ₽.
"""

from decimal import Decimal

import pytest

from claims_assistant.application.monitoring import Frequency
from claims_assistant.domain.monitoring_cost import (
    REQUESTS_PER_COMPANY,
    MonitoringCost,
    monitoring_cost,
)


@pytest.mark.parametrize(
    ("companies", "frequency", "expected"),
    [
        # A hundred every day: 500 a day, 400 paid — the 1 800 ₽ of the questions (S8-07).
        (100, "daily", MonitoringCost(500, Decimal(1800))),
        (100, "weekdays", MonitoringCost(500, Decimal(1290))),
        # A seventh of the list a day stays within the free hundred.
        (100, "weekly", MonitoringCost(71, Decimal(0))),
        # The customer's whole report: ≈ 6 700 ₽ daily, ≈ 570 ₽ weekly.
        (318, "daily", MonitoringCost(1590, Decimal(6710))),
        (318, "weekly", MonitoringCost(227, Decimal(570))),
        (20, "daily", MonitoringCost(100, Decimal(0))),
        (0, "daily", MonitoringCost(0, Decimal(0))),
    ],
)
def test_requests_and_rubles_a_month(companies, frequency, expected):
    assert monitoring_cost(companies, frequency) == expected


def test_a_small_sum_is_kept_in_rubles():
    """21 companies daily: 5 paid requests a day — 22,5 ₽ a month, not «20»."""
    assert monitoring_cost(21, "daily").rub_per_month == Decimal(23)


def test_every_frequency_of_the_service_has_a_price():
    for frequency in Frequency:
        assert monitoring_cost(10, frequency).requests_per_day >= 0
    with pytest.raises(ValueError):
        monitoring_cost(10, "monthly")


def test_a_company_costs_a_request_a_watched_section():
    assert REQUESTS_PER_COMPANY == 5
