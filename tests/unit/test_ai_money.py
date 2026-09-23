"""S5-03: the AI budget in roubles — per check and per month (decision of 23.09.2026).

The customer agreed on 1000 ₽ a month, so the money has to be counted, not estimated:
the provider reports the price of every call. Two limits are tested here — the one inside
a run, and the one that lives in storage and outlives the process.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.ai_guard import (
    AiBudget,
    AiPolicy,
    AiRunLimits,
    GuardedRecommendationProvider,
)
from claims_assistant.application.ai_spend import MonthlyLimit, month_of
from claims_assistant.application.recommendation import AiAnswer, AiErrorCode, AiUnavailable
from claims_assistant.domain.ai_context import build_request
from claims_assistant.infrastructure.llm.polza import PolzaRecommendationProvider
from claims_assistant.infrastructure.persistence.ai_spend import SqliteAiSpendStore
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from tests.unit.test_ai_evals import context
from tests.unit.test_polza_provider import KEY, LIMITS, Transport, answer

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def priced(cost: str | None) -> AiAnswer:
    return AiAnswer(
        text="{}",
        provider="polza",
        model="openai/gpt-4.1-mini",
        input_tokens=100,
        output_tokens=50,
        latency_seconds=1.0,
        cost_rub=None if cost is None else Decimal(cost),
    )


def budget(max_rub: str | None = "1.00") -> AiBudget:
    return AiBudget.for_run(AiRunLimits(max_rub=None if max_rub is None else Decimal(max_rub)), NOW)


def test_a_run_stops_asking_when_its_money_is_spent():
    spent = budget("0.20")
    assert spent.take_request(NOW) is True
    spent.charge(priced("0.11"))
    assert spent.take_request(NOW) is True  # 0,11 of 0,20 — still within
    spent.charge(priced("0.11"))
    assert spent.take_request(NOW) is False and spent.exhausted
    assert spent.spent_rub == Decimal("0.22")


def test_a_provider_that_names_no_price_is_bounded_by_requests_and_tokens():
    """Not knowing the price must not mean «free»: the other limits still hold."""
    free = budget("1.00")
    free.take_request(NOW)
    free.charge(priced(None))
    assert free.spent_rub == Decimal(0) and free.take_request(NOW) is True


def test_money_is_added_up_exactly():
    exact = budget("1.00")
    for _ in range(3):
        exact.charge(priced("0.0044424"))
    assert exact.spent_rub == Decimal("0.0133272")  # a float would already have drifted


async def test_the_price_of_a_call_comes_from_the_provider():
    provider = PolzaRecommendationProvider(KEY, transport=Transport())
    result = await provider.explain(build_request(context()), LIMITS)
    assert result.cost_rub == Decimal("0.0044424")


@pytest.mark.parametrize("usage", [{}, {"cost_rub": "0.01"}, {"cost_rub": -1}])
async def test_a_price_we_cannot_trust_is_no_price(usage):
    body = answer()
    body["usage"] = {**usage, "prompt_tokens": 1, "completion_tokens": 1}
    provider = PolzaRecommendationProvider(KEY, transport=Transport(200, body))
    result = await provider.explain(build_request(context()), LIMITS)
    assert result.cost_rub is None


async def test_the_guard_refuses_when_the_runs_money_is_gone():
    class Free:
        name, model = "stub", "stub-1"

        async def explain(self, request, limits):
            return priced("0.60")

    guard = GuardedRecommendationProvider(Free(), AiPolicy(), clock=lambda: NOW)
    run_budget = budget("1.00")
    request = build_request(context())
    await guard.explain_within(request, run_budget)
    with pytest.raises(AiUnavailable) as failure:
        await guard.explain_within(request, run_budget)
        await guard.explain_within(request, run_budget)
    assert failure.value.code is AiErrorCode.BUDGET


def test_what_is_left_of_the_month():
    month = MonthlyLimit(limit_rub=Decimal("1000"), spent_rub=Decimal("999.50"))
    assert month.left_rub == Decimal("0.50") and month.reached is False
    spent = MonthlyLimit(limit_rub=Decimal("1000"), spent_rub=Decimal("1000"))
    assert spent.reached is True and spent.left_rub == Decimal(0)
    over = MonthlyLimit(limit_rub=Decimal("1000"), spent_rub=Decimal("1200"))
    assert over.left_rub == Decimal(0)  # never negative


def test_the_month_is_the_calendar_month_in_utc():
    assert month_of(NOW) == "2026-09"
    assert month_of(datetime(2026, 12, 31, 23, 30, tzinfo=UTC)) == "2026-12"


async def test_the_monthly_spend_survives_a_restart(tmp_path):
    """The whole point of keeping it in storage rather than in the process."""
    path = tmp_path / "claims.sqlite3"
    repository = open_sqlite_repository(path)
    try:
        store = SqliteAiSpendStore(repository.engine)
        assert await store.spent("2026-09") == Decimal(0)
        assert await store.add("2026-09", Decimal("0.11")) == Decimal("0.11")
        assert await store.add("2026-09", Decimal("0.0044424")) == Decimal("0.1144424")
        assert await store.add("2026-10", Decimal("5")) == Decimal("5")  # months are apart
    finally:
        repository.close()

    again = open_sqlite_repository(path)
    try:
        assert await SqliteAiSpendStore(again.engine).spent("2026-09") == Decimal("0.1144424")
    finally:
        again.close()


async def test_a_call_that_cost_nothing_changes_nothing(tmp_path):
    repository = open_sqlite_repository(tmp_path / "claims.sqlite3")
    try:
        store = SqliteAiSpendStore(repository.engine)
        assert await store.add("2026-09", Decimal(0)) == Decimal(0)
        assert await store.spent("2026-09") == Decimal(0)
    finally:
        repository.close()


# --- the pipeline asks the month first (S5-03) -------------------------------------


class Spend:
    """The month's counter as the pipeline sees it."""

    def __init__(self, start=Decimal(0)):
        self.totals = {"2026-09": start}
        self.added: list[Decimal] = []

    async def spent(self, month: str) -> Decimal:
        return self.totals.get(month, Decimal(0))

    async def add(self, month: str, amount: Decimal) -> Decimal:
        self.added.append(amount)
        self.totals[month] = self.totals.get(month, Decimal(0)) + amount
        return self.totals[month]


async def test_a_spent_month_stops_the_model_before_the_first_question(tmp_path):
    from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
    from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
    from tests.unit.test_analysis_pipeline import NOW as RUN_NOW
    from tests.unit.test_analysis_pipeline import guard, pipeline
    from tests.unit.test_explanations import package

    run, files, repository = await package(tmp_path)
    model = StubRecommendationProvider()
    spend = Spend(start=Decimal("1000"))
    outcome = await pipeline(
        files,
        repository,
        guard(DemoCompanyDataProvider()),
        explainer=model,
        ai_spend=spend,
        ai_month_limit_rub=Decimal("1000"),
        clock=lambda: RUN_NOW,
    ).process(run)
    assert model.requests == []  # nothing was asked, so nothing was paid for
    assert "Лимит расходов на ИИ за месяц исчерпан" in (outcome.failure or "")
    assert spend.added == []


async def test_what_a_check_spent_is_added_to_the_month(tmp_path):
    from dataclasses import replace

    from claims_assistant.application.ai_guard import AiPolicy, GuardedRecommendationProvider
    from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
    from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
    from tests.unit.test_analysis_pipeline import NOW as RUN_NOW
    from tests.unit.test_analysis_pipeline import guard, pipeline
    from tests.unit.test_explanations import package

    class Priced(StubRecommendationProvider):
        """The stand-in model with a price tag, as the real provider reports one."""

        async def explain(self, request, limits):
            return replace(await super().explain(request, limits), cost_rub=Decimal("0.07"))

    run, files, repository = await package(tmp_path)
    spend = Spend()
    explainer = GuardedRecommendationProvider(Priced(), AiPolicy(), clock=lambda: RUN_NOW)
    await pipeline(
        files,
        repository,
        guard(DemoCompanyDataProvider()),
        explainer=explainer,
        ai_spend=spend,
        ai_month_limit_rub=Decimal("1000"),
        clock=lambda: RUN_NOW,
    ).process(run)
    assert spend.added == [Decimal("0.14")]  # two companies, 0,07 each
    assert spend.totals["2026-09"] == Decimal("0.14")
