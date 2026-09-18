"""Limits, retries and cache around CompanyDataProvider (S3-02)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from claims_assistant.application.company_data import CompanyDataRequest, RequestLimits
from claims_assistant.application.external_guard import (
    BUDGET_EXHAUSTED,
    GuardedCompanyDataProvider,
    GuardPolicy,
    RunBudget,
)
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.infrastructure.cache.memory import TtlSnapshotCache
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario

INN = "1234567894"
OTHER = "0000000000"
START = datetime(2026, 9, 19, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ScriptedProvider:
    """Answers per call from a script of (status, code) per section; counts calls."""

    def __init__(self, script: list[dict[Section, tuple[FetchStatus, str | None]]]) -> None:
        self.script = script
        self.calls: list[CompanyDataRequest] = []

    async def fetch(self, request: CompanyDataRequest):
        self.calls.append(request)
        plan = self.script.pop(0) if self.script else {}
        out = []
        for section in request.sections:
            status, code = plan.get(section, (FetchStatus.OK, None))
            if status is FetchStatus.OK:
                out.append(_ok(request.inn, section))
            else:
                out.append(_failed(request.inn, section, status, code or "x"))
        return tuple(out)


def _ok(inn: str, section: Section) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn, section, "scripted", DataMode.LIVE, START, FetchStatus.OK, Coverage.COMPLETE
    )


def _failed(inn: str, section: Section, status: FetchStatus, code: str) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn,
        section,
        "scripted",
        DataMode.LIVE,
        START,
        status,
        Coverage.UNAVAILABLE,
        missing=("сбой",),
        error=ProviderError(code, "сбой"),
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


def guard(inner, clock, sleeps, **policy) -> GuardedCompanyDataProvider:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    return GuardedCompanyDataProvider(
        inner,
        GuardPolicy(**policy),
        TtlSnapshotCache(ttl_seconds=3600, clock=clock),
        clock=clock,
        sleep=sleep,
    )


# --- retries ---


async def test_transient_failure_is_retried_only_for_failed_sections(clock, sleeps):
    inner = ScriptedProvider(
        [
            {Section.FINANCES: (FetchStatus.UNAVAILABLE, "timeout")},
            {},  # retry succeeds
        ]
    )
    provider = guard(inner, clock, sleeps, max_retries=2, backoff_seconds=1.0)
    snapshots = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert [s.status for s in snapshots] == [FetchStatus.OK] * 3
    assert inner.calls[1].sections == (Section.FINANCES,)
    assert sleeps == [1.0]


async def test_backoff_grows_and_retries_stop_at_the_limit(clock, sleeps):
    inner = ScriptedProvider(
        [
            {Section.COMPANY: (FetchStatus.RATE_LIMITED, "rate_limited")},
            {Section.COMPANY: (FetchStatus.UNAVAILABLE, "network_error")},
            {Section.COMPANY: (FetchStatus.UNAVAILABLE, "network_error")},
        ]
    )
    provider = guard(inner, clock, sleeps, max_retries=2, backoff_seconds=1.0)
    snapshots = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert snapshots[0].status is FetchStatus.UNAVAILABLE
    assert snapshots[0].error.code == "network_error"
    assert len(inner.calls) == 3
    assert sleeps == [1.0, 2.0]


async def test_retry_after_from_the_provider_is_honoured(clock, sleeps):
    inner = ScriptedProvider([{Section.COMPANY: (FetchStatus.RATE_LIMITED, "rate_limited")}, {}])
    original = inner.fetch

    async def with_retry_after(request):
        snapshots = await original(request)
        return tuple(
            replace(s, error=ProviderError("rate_limited", "сбой", retry_after_seconds=7.0))
            if s.error is not None
            else s
            for s in snapshots
        )

    inner.fetch = with_retry_after
    provider = guard(inner, clock, sleeps, max_retries=1, backoff_seconds=1.0)
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert sleeps == [7.0]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (FetchStatus.UNAUTHORIZED, "access_denied"),
        (FetchStatus.INVALID_RESPONSE, "invalid_response"),
        (FetchStatus.NOT_FOUND, "not_found"),
        (FetchStatus.UNAVAILABLE, "not_implemented"),
        (FetchStatus.UNAVAILABLE, "api_error"),
    ],
)
async def test_permanent_failures_are_not_retried(clock, sleeps, status, code):
    inner = ScriptedProvider([{Section.COMPANY: (status, code)}])
    provider = guard(inner, clock, sleeps, max_retries=2)
    snapshots = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert snapshots[0].status is status
    assert len(inner.calls) == 1 and sleeps == []


# --- cache ---


async def test_successful_sections_are_served_from_cache_within_ttl(clock, sleeps):
    inner = ScriptedProvider([{}, {}])
    provider = guard(inner, clock, sleeps)
    first = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    clock.advance(600)
    second = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert len(inner.calls) == 1
    assert second == first  # same snapshots, same fetched_at: the report shows the cache date
    assert all(s.fetched_at == START for s in second)


async def test_cache_expires_after_ttl_and_is_keyed_by_inn_and_section(clock, sleeps):
    inner = ScriptedProvider([{}, {}, {}])
    provider = guard(inner, clock, sleeps)
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(OTHER))
    assert len(inner.calls) == 2
    clock.advance(3601)
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert len(inner.calls) == 3


async def test_failed_sections_are_not_cached(clock, sleeps):
    inner = ScriptedProvider([{Section.FINANCES: (FetchStatus.UNAUTHORIZED, "access_denied")}, {}])
    provider = guard(inner, clock, sleeps)
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    again = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert inner.calls[1].sections == (Section.FINANCES,)
    assert again[2].status is FetchStatus.OK


async def test_partial_cache_hit_requests_only_missing_sections(clock, sleeps):
    inner = ScriptedProvider([{}, {}])
    provider = guard(inner, clock, sleeps)
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN, (Section.COMPANY,)))
    snapshots = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert inner.calls[1].sections == (Section.BANKRUPTCY, Section.FINANCES)
    assert [s.section for s in snapshots] == list(Section)


# --- run budget ---


async def test_request_budget_marks_remaining_sections_exhausted(clock, sleeps):
    inner = ScriptedProvider([{}, {}, {}])
    provider = guard(inner, clock, sleeps)
    budget = RunBudget(max_requests=4, deadline=START + timedelta(minutes=15))
    scoped = provider.scoped(budget)
    first = await scoped.fetch(CompanyDataRequest(INN))  # 3 sections = 3 requests
    assert all(s.status is FetchStatus.OK for s in first)
    second = await scoped.fetch(CompanyDataRequest(OTHER))
    assert second[0].status is FetchStatus.OK
    assert [s.error.code for s in second[1:]] == [BUDGET_EXHAUSTED] * 2
    assert all(s.coverage is Coverage.UNAVAILABLE for s in second[1:])
    assert budget.exhausted is True


async def test_time_budget_stops_new_requests(clock, sleeps):
    inner = ScriptedProvider([{}, {}])
    provider = guard(inner, clock, sleeps)
    budget = RunBudget(max_requests=1000, deadline=START + timedelta(minutes=15))
    scoped = provider.scoped(budget)
    await scoped.fetch(CompanyDataRequest(INN))
    clock.advance(16 * 60)
    late = await scoped.fetch(CompanyDataRequest(OTHER))
    assert all(s.error.code == BUDGET_EXHAUSTED for s in late)
    assert len(inner.calls) == 1


async def test_cache_hits_do_not_consume_the_budget(clock, sleeps):
    inner = ScriptedProvider([{}])
    provider = guard(inner, clock, sleeps)
    budget = RunBudget(max_requests=3, deadline=START + timedelta(minutes=15))
    scoped = provider.scoped(budget)
    await scoped.fetch(CompanyDataRequest(INN))
    again = await scoped.fetch(CompanyDataRequest(INN))
    assert all(s.status is FetchStatus.OK for s in again)
    assert budget.used == 3


async def test_retries_count_against_the_budget(clock, sleeps):
    inner = ScriptedProvider([{Section.COMPANY: (FetchStatus.UNAVAILABLE, "timeout")}, {}, {}])
    provider = guard(inner, clock, sleeps, max_retries=2, backoff_seconds=0.1)
    budget = RunBudget(max_requests=4, deadline=START + timedelta(minutes=15))
    await provider.scoped(budget).fetch(CompanyDataRequest(INN))
    assert budget.used == 4


async def test_request_limits_from_policy_reach_the_inner_provider(clock, sleeps):
    inner = ScriptedProvider([{}])
    provider = guard(inner, clock, sleeps, timeout_seconds=5, max_pages=3)
    await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert inner.calls[0].limits == RequestLimits(timeout_seconds=5, max_requests=20, max_pages=3)


async def test_programming_errors_and_cancellation_propagate(clock, sleeps):
    class Broken:
        async def fetch(self, request):
            raise RuntimeError("bug")

    provider = guard(Broken(), clock, sleeps)
    with pytest.raises(RuntimeError):
        await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))


async def test_demo_provider_works_through_the_guard(clock, sleeps):
    provider = guard(DemoCompanyDataProvider(DemoScenario.ALARM), clock, sleeps)
    snapshots = await provider.scoped(RunBudget.unlimited()).fetch(CompanyDataRequest(INN))
    assert [s.section for s in snapshots] == list(Section)
    assert snapshots[1].facts


def test_policy_and_budget_validate_their_numbers():
    with pytest.raises(ValueError):
        GuardPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        GuardPolicy(backoff_seconds=0)
    with pytest.raises(ValueError):
        RunBudget(max_requests=0, deadline=START)
    with pytest.raises(ValueError):
        RunBudget(max_requests=1, deadline=datetime(2026, 9, 19, 12))


async def test_plain_fetch_works_for_a_single_card_with_its_own_budget(clock, sleeps):
    inner = ScriptedProvider([{}, {}])
    provider = guard(inner, clock, sleeps, single_check_requests=2)
    snapshots = await provider.fetch(CompanyDataRequest(INN))
    assert [s.status for s in snapshots] == [
        FetchStatus.OK,
        FetchStatus.OK,
        FetchStatus.UNAVAILABLE,
    ]
    assert snapshots[2].error.code == BUDGET_EXHAUSTED
    # A new card gets a fresh budget (the cache serves the first two sections).
    again = await provider.fetch(CompanyDataRequest(INN))
    assert [s.status for s in again] == [FetchStatus.OK] * 3


async def test_exhausted_sections_follow_the_provider_mode(clock, sleeps):
    inner = DemoCompanyDataProvider(DemoScenario.ORDINARY)
    provider = GuardedCompanyDataProvider(
        inner,
        GuardPolicy(),
        TtlSnapshotCache(ttl_seconds=0, clock=clock),
        mode=DataMode.DEMO,
        clock=clock,
    )
    budget = RunBudget(max_requests=1, deadline=START + timedelta(minutes=1))
    snapshots = await provider.scoped(budget).fetch(CompanyDataRequest(INN))
    assert {s.mode for s in snapshots} == {DataMode.DEMO}
    from claims_assistant.application.check_company import CompanyCheck

    CompanyCheck(INN, snapshots)  # mixed modes would raise here
