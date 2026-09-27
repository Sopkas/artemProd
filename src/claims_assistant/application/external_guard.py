"""Limits, retries and cache around any CompanyDataProvider (S3-02).

Per run: a time deadline and a request budget shared by all companies of the check.
Per section: transient failures are retried with growing pauses (or the provider's
Retry-After), successful snapshots are cached by (inn, section) for a while so the same
company is not paid for twice. The guard never turns a failure into a demo answer and
never raises for expected source trouble; programming errors and cancellation propagate.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)

from .company_data import CompanyDataProvider, CompanyDataRequest, RequestLimits

# docs/architecture.md: 20 s per request, up to 2 retries, 20 pages,
# 15 minutes and 1 500 attempts per run.
BUDGET_EXHAUSTED = "budget_exhausted"
BUDGET_MESSAGE = "Лимит времени или запросов проверки исчерпан; раздел не проверен."
TRANSIENT_CODES = frozenset({"timeout", "network_error", "http_error", "rate_limited"})
SOURCE_LIMIT = "source_limit"
SOURCE_LIMIT_MESSAGE = (
    "Источник данных ответил, что лимит запросов исчерпан; раздел не проверен. "
    "Повторите проверку позже."
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    timeout_seconds: float = 20
    max_pages: int = 20
    max_retries: int = 2
    backoff_seconds: float = 1.0
    # Budget of one interactive card (plain fetch): a few sections with retries.
    single_check_requests: int = 20
    single_check_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.single_check_requests <= 0 or self.single_check_seconds <= 0:
            raise ValueError("single check budget must be positive")
        if self.backoff_seconds <= 0:
            raise ValueError("backoff_seconds must be positive")
        RequestLimits(timeout_seconds=self.timeout_seconds, max_pages=self.max_pages)

    @property
    def request_limits(self) -> RequestLimits:
        return RequestLimits(timeout_seconds=self.timeout_seconds, max_pages=self.max_pages)


@dataclass(slots=True)
class RunBudget:
    """Shared by every company of one check; one unit ≈ one external request."""

    max_requests: int
    deadline: datetime
    used: int = 0
    exhausted: bool = field(default=False, init=False)
    # The source itself said «enough» (still 429 after the retries): on the free tariff
    # that is the daily limit, and every next company of this check would get the same.
    source_closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if self.deadline.utcoffset() is None:
            raise ValueError("deadline must include a timezone")

    @classmethod
    def unlimited(cls) -> "RunBudget":
        return cls(max_requests=10**9, deadline=datetime.max.replace(tzinfo=UTC))

    @classmethod
    def for_run(cls, max_requests: int, max_seconds: float, now: datetime) -> "RunBudget":
        return cls(max_requests=max_requests, deadline=now + timedelta(seconds=max_seconds))

    def take(self, units: int, now: datetime) -> int:
        """Reserve up to `units` requests; returns how many were granted."""
        if now >= self.deadline:
            self.exhausted = True
            return 0
        granted = max(0, min(units, self.max_requests - self.used))
        self.used += granted
        if granted < units:
            self.exhausted = True
        return granted


class SnapshotCache(Protocol):
    def get(self, inn: str, section: Section) -> ExternalSnapshot | None: ...

    def put(self, snapshot: ExternalSnapshot) -> None: ...


class GuardedCompanyDataProvider:
    def __init__(
        self,
        inner: CompanyDataProvider,
        policy: GuardPolicy,
        cache: SnapshotCache,
        *,
        mode: DataMode = DataMode.LIVE,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.inner = inner
        self._policy = policy
        self._cache = cache
        self._mode = mode  # the mode of the inner provider, used for budget placeholders
        self._clock = clock
        self._sleep = sleep

    def scoped(self, budget: RunBudget) -> CompanyDataProvider:
        return _ScopedProvider(self, budget)

    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        """One interactive card: a fresh, small budget per call."""
        budget = RunBudget.for_run(
            self._policy.single_check_requests,
            self._policy.single_check_seconds,
            self._clock(),
        )
        return await self.fetch_within(request, budget)

    async def fetch_within(
        self, request: CompanyDataRequest, budget: RunBudget
    ) -> tuple[ExternalSnapshot, ...]:
        results: dict[Section, ExternalSnapshot] = {}
        pending: list[Section] = []
        for section in request.sections:
            cached = self._cache.get(request.inn, section)
            if cached is not None:
                results[section] = cached
            else:
                pending.append(section)

        if budget.source_closed:
            for section in pending:
                results[section] = _source_limited(request.inn, section, self._mode, self._clock())
            pending = []

        attempt = 0
        while pending:
            granted = budget.take(len(pending), self._clock())
            if granted < len(pending):
                for section in pending[granted:]:
                    results[section] = _exhausted(request.inn, section, self._mode, self._clock())
                pending = pending[:granted]
                if not pending:
                    break
            inner_request = CompanyDataRequest(
                inn=request.inn, sections=tuple(pending), limits=self._policy.request_limits
            )
            snapshots = await self.inner.fetch(inner_request)
            retry: list[Section] = []
            wait: float | None = None
            for snapshot in snapshots:
                if snapshot.status is FetchStatus.OK:
                    self._cache.put(snapshot)
                    results[snapshot.section] = snapshot
                elif _is_transient(snapshot) and attempt < self._policy.max_retries:
                    retry.append(snapshot.section)
                    results[snapshot.section] = snapshot
                    hint = snapshot.error.retry_after_seconds if snapshot.error else None
                    if hint is not None:
                        wait = max(wait or 0.0, hint)
                else:
                    results[snapshot.section] = snapshot
            if not retry:
                break
            attempt += 1
            await self._sleep(
                wait if wait is not None else self._policy.backoff_seconds * 2 ** (attempt - 1)
            )
            pending = retry

        if any(snapshot.status is FetchStatus.RATE_LIMITED for snapshot in results.values()):
            budget.source_closed = True
        return tuple(results[section] for section in request.sections)


class _ScopedProvider:
    def __init__(self, guard: GuardedCompanyDataProvider, budget: RunBudget) -> None:
        self._guard = guard
        self._budget = budget

    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        return await self._guard.fetch_within(request, self._budget)


def _is_transient(snapshot: ExternalSnapshot) -> bool:
    if snapshot.status is FetchStatus.RATE_LIMITED:
        return True
    return snapshot.error is not None and snapshot.error.code in TRANSIENT_CODES


def _source_limited(inn: str, section: Section, mode: DataMode, now: datetime) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn=inn,
        section=section,
        source="guard",
        mode=mode,
        fetched_at=now,
        status=FetchStatus.RATE_LIMITED,
        coverage=Coverage.UNAVAILABLE,
        missing=(SOURCE_LIMIT_MESSAGE,),
        error=ProviderError(SOURCE_LIMIT, SOURCE_LIMIT_MESSAGE),
    )


def _exhausted(inn: str, section: Section, mode: DataMode, now: datetime) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn=inn,
        section=section,
        source="guard",
        mode=mode,
        fetched_at=now,
        status=FetchStatus.UNAVAILABLE,
        coverage=Coverage.UNAVAILABLE,
        missing=(BUDGET_MESSAGE,),
        error=ProviderError(BUDGET_EXHAUSTED, BUDGET_MESSAGE),
    )
