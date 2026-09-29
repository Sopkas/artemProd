"""What the model has cost us, in roubles (S5-03, decision of 23.09.2026).

Until now the budget was counted in requests, tokens and seconds — everything except the
thing the customer actually agreed on: **1000 ₽ a month**. The provider reports the price
of every call (``usage.cost_rub``), so the money can be counted exactly instead of being
estimated from tokens and a tariff.

Two limits, deliberately different in nature:

- **per run** — kept in the run's own budget (``AiRunLimits.max_rub``), so a single check
  cannot spend the month in one go, whatever the package's size;
- **per month** — kept in storage, because it has to survive restarts and be shared by
  every check of every user. It is read once, before a check starts asking, and the
  check's spend is added when it is over. A run may therefore overshoot the month by at
  most one run limit, and that is the price of not taking a lock around every call.

The month is the calendar month in UTC: the same boundary the provider's console shows,
and one that does not shift with the operator's time zone.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


def month_of(moment: datetime) -> str:
    """The key a month is stored under: «2026-09»."""
    return f"{moment.year:04d}-{moment.month:02d}"


class AiSpendStore(Protocol):
    """Where the monthly spend lives; the infrastructure supplies it."""

    async def spent(self, month: str) -> Decimal:
        """Roubles spent on the model in that month; zero when nothing is recorded."""
        ...

    async def add(self, month: str, amount: Decimal) -> Decimal:
        """Add to the month and return the new total; a non-positive amount changes nothing."""
        ...


@dataclass(frozen=True, slots=True)
class MonthlyLimit:
    """The customer's agreed ceiling, and what is left of it."""

    limit_rub: Decimal
    spent_rub: Decimal

    @property
    def left_rub(self) -> Decimal:
        return max(Decimal(0), self.limit_rub - self.spent_rub)

    @property
    def reached(self) -> bool:
        return self.spent_rub >= self.limit_rub


NO_MONEY_LEFT = (
    "Лимит расходов на ИИ за месяц исчерпан; пояснения не запрашивались, "
    "в отчёте — рекомендации по правилам."
)
