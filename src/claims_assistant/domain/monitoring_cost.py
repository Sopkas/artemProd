"""S8-12 (part B): what a monitored list costs at the source, by how often it is checked.

The customer asked for a pause «чтобы не тратить деньги» (17.10.2026); the frequency
buttons show the price of each choice next to it. Pure arithmetic: the tariff is Checko's
API «Стандарт» as the customer is buying it — the first hundred requests a day free, then
0,15 ₽ each, only successful answers paid (checked on the site 16.10.2026, S8-07).

An estimate, not a bill: a month is 30 days, the weekly mode spreads the list evenly over
the week (``application.monitoring.weekly_slot`` takes the INN modulo 7 — close to even on
a long list), and the /inn cards share the same free hundred — they are not counted here.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from claims_assistant.domain.monitoring import MONITORED

__all__ = [
    "FREE_PER_DAY",
    "PRICE_RUB",
    "REQUESTS_PER_COMPANY",
    "MonitoringCost",
    "monitoring_cost",
]

FREE_PER_DAY = 100
PRICE_RUB = Decimal("0.15")
# One request a section; finances are not watched (domain.monitoring.MONITORED).
REQUESTS_PER_COMPANY = len(MONITORED)
_DAYS = Decimal(30)
_WEEK = Decimal(7)
# Checked days a month: every day, five of seven, or every day a seventh of the list.
_RUNS = {"daily": _DAYS, "weekdays": _DAYS * 5 / _WEEK, "weekly": _DAYS}
_SHARE = {"daily": Decimal(1), "weekdays": Decimal(1), "weekly": 1 / _WEEK}


@dataclass(frozen=True, slots=True)
class MonitoringCost:
    # On a day the list is looked at; in the weekly mode — an average day.
    requests_per_day: int
    rub_per_month: Decimal  # rounded: to whole rubles under a hundred, to tens above

    @property
    def free(self) -> bool:
        return self.rub_per_month == 0


def _round(value: Decimal) -> Decimal:
    step = Decimal(1) if value < 100 else Decimal(10)
    return (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def monitoring_cost(companies: int, frequency: str) -> MonitoringCost:
    """``frequency`` is a value of ``application.monitoring.Frequency``: «daily»,
    «weekdays» or «weekly»."""
    if frequency not in _RUNS:
        raise ValueError(f"unknown frequency: {frequency}")
    per_day = Decimal(companies * REQUESTS_PER_COMPANY) * _SHARE[frequency]
    paid = max(Decimal(0), per_day - FREE_PER_DAY)
    rub = _round(paid * PRICE_RUB * _RUNS[frequency])
    return MonitoringCost(int(per_day.to_integral_value(rounding=ROUND_HALF_UP)), rub)
