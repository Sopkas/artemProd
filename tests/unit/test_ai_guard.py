"""S5-03: timeouts, one retry, request/answer bounds and a per-run AI budget."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from claims_assistant.application.ai_guard import (
    AiBudget,
    AiPolicy,
    AiRunLimits,
    GuardedRecommendationProvider,
)
from claims_assistant.application.recommendation import (
    AiErrorCode,
    AiLimits,
    AiUnavailable,
    RecommendationProvider,
)
from claims_assistant.domain.ai_context import build_request
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
from tests.unit.test_analysis_pipeline import NOW as PIPELINE_NOW
from tests.unit.test_analysis_pipeline import (
    OWNER,
    guard,
    pipeline,
)
from tests.unit.test_explanations import explanation_steps, package
from tests.unit.test_recommendation import context_for

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class Flaky:
    """Fails the first N calls with the given code, then answers like the stub."""

    name = "flaky"
    model = "flaky-1"

    def __init__(self, failures: int, code: AiErrorCode = AiErrorCode.UNAVAILABLE) -> None:
        self.failures = failures
        self.code = code
        self.calls = 0

    async def explain(self, request, limits):
        self.calls += 1
        if self.calls <= self.failures:
            raise AiUnavailable(self.code, "Сбой.")
        return await StubRecommendationProvider().explain(request, limits)


async def no_sleep(_seconds: float) -> None:
    return None


def guarded(inner, clock=lambda: NOW, **policy):
    return GuardedRecommendationProvider(inner, AiPolicy(**policy), clock=clock, sleep=no_sleep)


def test_guard_is_a_provider_and_carries_the_inner_names():
    provider = guarded(StubRecommendationProvider())
    assert isinstance(provider, RecommendationProvider)
    assert (provider.name, provider.model) == ("stub", "stub-1")


async def test_transient_failure_is_retried_once_then_given_up(caplog):
    context = await context_for()
    request = build_request(context)
    once = Flaky(1)
    with caplog.at_level(logging.INFO):
        answer = await guarded(once, max_retries=1).explain(request, AiLimits())
    assert answer.text and once.calls == 2
    assert "ai_retry" in caplog.text
    twice = Flaky(2)
    with pytest.raises(AiUnavailable) as failure:
        await guarded(twice, max_retries=1).explain(request, AiLimits())
    assert failure.value.code is AiErrorCode.UNAVAILABLE and twice.calls == 2


@pytest.mark.parametrize("code", [AiErrorCode.REFUSED, AiErrorCode.BUDGET])
async def test_final_failures_are_not_retried(code):
    context = await context_for()
    request = build_request(context)
    inner = Flaky(1, code)
    with pytest.raises(AiUnavailable) as failure:
        await guarded(inner, max_retries=1).explain(request, AiLimits())
    assert failure.value.code is code and inner.calls == 1


async def test_a_slow_model_is_cut_by_the_guard_timeout():
    class Sleeper:
        name = "sleeper"
        model = "s"

        async def explain(self, request, limits):
            await asyncio.sleep(5)

    context = await context_for()
    request = build_request(context)
    with pytest.raises(AiUnavailable) as failure:
        await guarded(Sleeper(), timeout_seconds=0.05, max_retries=0).explain(request, AiLimits())
    assert failure.value.code is AiErrorCode.TIMEOUT


async def test_oversized_request_never_reaches_the_model():
    context = await context_for()
    request = build_request(context)
    inner = StubRecommendationProvider()
    with pytest.raises(AiUnavailable) as failure:
        await guarded(inner, max_request_chars=50).explain(request, AiLimits())
    assert failure.value.code is AiErrorCode.BUDGET and inner.requests == []


async def test_run_budget_counts_requests_and_tokens_and_stops_the_rest():
    context = await context_for()
    request = build_request(context)
    inner = StubRecommendationProvider()
    scoped = guarded(inner).scoped(
        AiBudget(max_requests=2, max_tokens=10**9, deadline=NOW + timedelta(hours=1))
    )
    await scoped.explain(request, AiLimits())
    await scoped.explain(request, AiLimits())
    with pytest.raises(AiUnavailable) as failure:
        await scoped.explain(request, AiLimits())
    assert failure.value.code is AiErrorCode.BUDGET and len(inner.requests) == 2
    assert scoped.budget.requests == 2 and scoped.budget.tokens > 0 and scoped.budget.exhausted

    # Tokens: the first answer spends the whole budget, the second call is refused.
    inner = StubRecommendationProvider()
    scoped = guarded(inner).scoped(
        AiBudget(max_requests=10, max_tokens=1, deadline=NOW + timedelta(hours=1))
    )
    await scoped.explain(request, AiLimits())
    with pytest.raises(AiUnavailable):
        await scoped.explain(request, AiLimits())
    assert len(inner.requests) == 1


async def test_run_deadline_is_measured_by_the_guard_clock():
    context = await context_for()
    request = build_request(context)
    ticks = iter([NOW + timedelta(seconds=30), NOW + timedelta(seconds=90)])
    provider = GuardedRecommendationProvider(
        StubRecommendationProvider(), AiPolicy(), clock=lambda: next(ticks), sleep=no_sleep
    )
    scoped = provider.scoped(AiBudget.for_run(AiRunLimits(max_seconds=60), NOW))
    await scoped.explain(request, AiLimits())  # at +30 s
    with pytest.raises(AiUnavailable) as failure:
        await scoped.explain(request, AiLimits())  # at +90 s
    assert failure.value.code is AiErrorCode.BUDGET


async def test_retries_count_against_the_run_budget():
    context = await context_for()
    request = build_request(context)
    inner = Flaky(1)
    scoped = guarded(inner, max_retries=1).scoped(
        AiBudget(max_requests=1, max_tokens=10**9, deadline=NOW + timedelta(hours=1))
    )
    with pytest.raises(AiUnavailable) as failure:
        await scoped.explain(request, AiLimits())
    assert failure.value.code is AiErrorCode.BUDGET and inner.calls == 1


async def test_pipeline_finishes_partial_when_the_ai_budget_runs_out(tmp_path):
    """Two companies, a budget of one request: the second gets no explanation, the
    check still completes with the report and says what is missing."""
    run, files, repository = await package(tmp_path)
    inner = StubRecommendationProvider()
    explainer = guarded(inner, clock=lambda: PIPELINE_NOW)  # the run budget uses the pipeline clock
    outcome = await pipeline(
        files,
        repository,
        guard(DemoCompanyDataProvider()),
        explainer=explainer,
        ai_run_limits=AiRunLimits(max_requests=1),
    ).process(run)
    assert outcome.status.value == "partial"
    assert "Пояснений ИИ нет у 1 организаций" in outcome.failure
    assert len(inner.requests) == 1
    assert len(explanation_steps(await repository.list_steps(run.id))) == 1
    assert await repository.get_report(OWNER, run.id) is not None


async def test_pipeline_with_a_full_budget_completes_and_the_summary_says_so(tmp_path):
    run, files, repository = await package(tmp_path)
    explainer = guarded(StubRecommendationProvider(), clock=lambda: PIPELINE_NOW)
    outcome = await pipeline(
        files, repository, guard(DemoCompanyDataProvider()), explainer=explainer
    ).process(run)
    assert outcome.status.value == "completed"
    from claims_assistant.application.analysis_pipeline import load_summary

    summary = await load_summary(run.id, repository)
    assert summary.explanations_missing == 0


def test_policy_and_run_limits_must_be_sane():
    with pytest.raises(ValueError):
        AiPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        AiPolicy(timeout_seconds=0)
    with pytest.raises(ValueError):
        AiRunLimits(max_tokens=0)


def test_scoped_factory_is_checked_by_type_not_by_name():
    from claims_assistant.application.ai_guard import ScopedExplainerFactory

    assert isinstance(guarded(StubRecommendationProvider()), ScopedExplainerFactory)
    assert not isinstance(StubRecommendationProvider(), ScopedExplainerFactory)


def test_default_request_size_is_the_same_everywhere():
    assert AiLimits().max_request_chars == AiPolicy().max_request_chars
