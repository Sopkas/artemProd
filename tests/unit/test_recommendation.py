"""S5-01: the provider boundary and the stand-in model, judged by the S5-06 review."""

import json
import logging
from datetime import date
from decimal import Decimal

import pytest

from claims_assistant.application.recommendation import (
    AiAnswer,
    AiErrorCode,
    AiLimits,
    AiUnavailable,
    RecommendationProvider,
    request_explanation,
    request_size,
)
from claims_assistant.domain.ai_context import (
    CONTEXT_VERSION,
    INSTRUCTION_VERSION,
    ContextLimits,
    build_context,
    build_request,
)
from claims_assistant.domain.ai_review import ANSWER_SCHEMA_VERSION, RejectionCode, review_answer
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.scoring import assess
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider, default_answer

INN = "7707083893"
DAY = date(2026, 9, 1)


async def context_for(scenario=DemoScenario.ORDINARY, row=None, comments=True):
    from claims_assistant.application.company_data import CompanyDataRequest

    row = row or CounterpartyRow(
        inn=INN,
        cutoff_date=DAY,
        debt=Decimal("1000.00"),
        overdue_days=75,
        last_payment_date=date(2026, 6, 1),
    )
    snaps = await DemoCompanyDataProvider(scenario).fetch(CompanyDataRequest(INN))
    assessment = assess(INN, snaps, row, analysis_date=DAY)
    interactions = (
        InteractionRow(INN, "INT-1", date(2026, 8, 20), "Обещали оплатить до 30.09.2026.", "тел."),
    )
    return build_context(
        row,
        assessment,
        DAY,
        interactions=interactions,
        snapshots=snaps,
        limits=ContextLimits(include_comments=comments),
        reference="row-1",
    )


def test_stub_satisfies_the_provider_protocol():
    provider = StubRecommendationProvider()
    assert isinstance(provider, RecommendationProvider)
    assert provider.name == "stub" and provider.model


@pytest.mark.parametrize("scenario", list(DemoScenario))
async def test_stub_answer_passes_the_review_for_every_demo_scenario(scenario):
    context = await context_for(scenario)
    request = build_request(context)
    answer = default_answer(request)
    reviewed = review_answer(answer, context)
    assert not hasattr(reviewed, "code"), getattr(reviewed, "message", None)
    assert reviewed.text and set(reviewed.grounds) <= {
        fact_id for signal in context.signals for fact_id in signal.fact_ids
    }


async def test_unknown_priority_answer_passes_too():
    row = CounterpartyRow(inn=INN)  # nothing internal, nothing to fire on
    context = await context_for(DemoScenario.INCOMPLETE, row=row)
    assert context.priority == "unknown"
    reviewed = review_answer(default_answer(build_request(context)), context)
    assert not hasattr(reviewed, "code")


async def test_accepted_outcome_carries_answer_versions_and_cost(caplog):
    context = await context_for()
    provider = StubRecommendationProvider(delay_seconds=0.01)
    with caplog.at_level(logging.INFO):
        outcome = await request_explanation(provider, context)
    assert outcome.status == "accepted"
    assert outcome.explanation is not None and outcome.rejected is None
    assert outcome.answer.provider == "stub" and outcome.answer.input_tokens > 0
    assert outcome.answer.latency_seconds == pytest.approx(0.01)
    assert outcome.versions == (
        f"stub:stub-1:i{INSTRUCTION_VERSION}:c{CONTEXT_VERSION}:s{ANSWER_SCHEMA_VERSION}"
        f":r{context.rules_version}"
    )
    assert "ai_answer_accepted" in caplog.text
    # The request the stub saw names the counterparty by the reference.
    assert provider.requests[0].context["counterparty_ref"] == "row-1"
    assert "inn" not in provider.requests[0].context


async def test_request_body_does_not_contain_the_inn_anywhere():
    """Raised by A on #40: provider ids embed the INN («<ИНН>:company:status»), so
    keeping it out of the named field was not enough. build_context masks it now."""
    context = await context_for()
    body = json.dumps(build_request(context).context, ensure_ascii=False)
    assert INN not in body


async def test_rejected_answer_is_reported_with_its_code_not_raised(caplog):
    context = await context_for()
    provider = StubRecommendationProvider(
        script=lambda request: json.dumps(
            {"explanation": "Долг вырос до 250000 руб.", "promises": [], "grounds": []}
        )
    )
    with caplog.at_level(logging.INFO):
        outcome = await request_explanation(provider, context)
    assert outcome.status == f"rejected:{RejectionCode.NEW_AMOUNT}"
    assert outcome.rejected.code is RejectionCode.NEW_AMOUNT and outcome.answer is not None
    assert "ai_answer_rejected" in caplog.text and "250000" not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        AiUnavailable(AiErrorCode.UNAVAILABLE, "Сервис недоступен."),
        AiUnavailable(AiErrorCode.RATE_LIMITED, "Слишком много запросов."),
        AiUnavailable(AiErrorCode.REFUSED, "Запрос отклонён провайдером."),
    ],
)
async def test_expected_provider_failures_become_outcomes(failure, caplog):
    context = await context_for()
    provider = StubRecommendationProvider(failure=failure)
    with caplog.at_level(logging.WARNING):
        outcome = await request_explanation(provider, context)
    assert outcome.status == f"unavailable:{failure.code}"
    assert outcome.error is failure and outcome.answer is None
    assert "ai_unavailable" in caplog.text


async def test_slow_model_is_a_timeout_within_the_limits():
    context = await context_for()
    provider = StubRecommendationProvider(delay_seconds=5)
    outcome = await request_explanation(provider, context, AiLimits(timeout_seconds=1))
    assert outcome.status == "unavailable:timeout"


async def test_oversized_request_is_not_sent():
    context = await context_for()
    provider = StubRecommendationProvider()
    outcome = await request_explanation(provider, context, AiLimits(max_request_chars=100))
    assert outcome.status == "unavailable:budget"
    assert provider.requests == []  # nothing left the process
    assert request_size(build_request(context)) > 100


async def test_programming_errors_in_a_provider_propagate():
    class Broken:
        name = "broken"
        model = "x"

        async def explain(self, request, limits) -> AiAnswer:
            raise KeyError("bug")

    context = await context_for()
    with pytest.raises(KeyError):
        await request_explanation(Broken(), context)


def test_limits_must_be_positive():
    with pytest.raises(ValueError):
        AiLimits(timeout_seconds=0)
    with pytest.raises(ValueError):
        AiLimits(max_output_tokens=0)
