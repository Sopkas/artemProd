"""Limits around any recommendation provider (S5-03).

Like the external-data guard (S3-02), this wraps a provider and enforces what the
architecture promises: one main call and at most one retry of a transient failure, a
timeout per call, a bound on the request and the answer, and a budget shared by every
company of one check — requests and tokens. When the budget is gone the guard answers
``AiUnavailable(budget)`` without calling the model, so the check still finishes: the
report carries the rules' recommendation for the rest (S5-04) and says so.

Tokens are the provider's own count; the money limit follows from them and the tariff
of the chosen provider, and is set per run through ``AiRunLimits`` (S6-05 measures it).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from claims_assistant.domain.ai_context import Request

from .recommendation import (
    AiAnswer,
    AiErrorCode,
    AiLimits,
    AiUnavailable,
    RecommendationProvider,
    request_size,
)

logger = logging.getLogger(__name__)

RETRIABLE = frozenset({AiErrorCode.TIMEOUT, AiErrorCode.UNAVAILABLE, AiErrorCode.RATE_LIMITED})
BUDGET_MESSAGE = "Лимит запросов, токенов или времени ИИ на проверку исчерпан."


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AiPolicy:
    """Per-call limits: the architecture's starting values (docs/architecture.md)."""

    timeout_seconds: float = 30.0
    max_retries: int = 1  # one main call and at most one retry
    backoff_seconds: float = 2.0
    max_request_chars: int = 24_000  # ≈ 6 000 tokens of input
    max_output_tokens: int = 1_000

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.backoff_seconds <= 0:
            raise ValueError("backoff_seconds must be positive")
        AiLimits(
            timeout_seconds=self.timeout_seconds,
            max_request_chars=self.max_request_chars,
            max_output_tokens=self.max_output_tokens,
        )

    @property
    def limits(self) -> AiLimits:
        return AiLimits(
            timeout_seconds=self.timeout_seconds,
            max_request_chars=self.max_request_chars,
            max_output_tokens=self.max_output_tokens,
        )


@dataclass(frozen=True, slots=True)
class AiRunLimits:
    """Budget of one check shared by all its companies."""

    max_requests: int = 100  # 50 companies, each with one retry
    max_tokens: int = 400_000  # input + output, as counted by the provider
    max_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.max_requests <= 0 or self.max_tokens <= 0 or self.max_seconds <= 0:
            raise ValueError("AI run limits must be positive")


@dataclass(slots=True)
class AiBudget:
    max_requests: int
    max_tokens: int
    deadline: datetime
    requests: int = 0
    tokens: int = 0
    exhausted: bool = field(default=False, init=False)

    @classmethod
    def for_run(cls, limits: AiRunLimits, now: datetime) -> "AiBudget":
        return cls(
            max_requests=limits.max_requests,
            max_tokens=limits.max_tokens,
            deadline=now + timedelta(seconds=limits.max_seconds),
        )

    @classmethod
    def unlimited(cls) -> "AiBudget":
        return cls(10**9, 10**12, datetime.max.replace(tzinfo=UTC))

    def take_request(self, now: datetime) -> bool:
        """Reserve one request; False (and exhausted) when nothing is left."""
        if self.exhausted or now >= self.deadline:
            self.exhausted = True
            return False
        if self.requests >= self.max_requests or self.tokens >= self.max_tokens:
            self.exhausted = True
            return False
        self.requests += 1
        return True

    def charge(self, answer: AiAnswer) -> None:
        self.tokens += answer.input_tokens + answer.output_tokens
        if self.tokens >= self.max_tokens:
            self.exhausted = True


class ScopedExplainerFactory(Protocol):
    """A guarded provider: one shared budget for every company of a run."""

    name: str
    model: str

    def scoped(self, budget: AiBudget) -> RecommendationProvider: ...


class GuardedRecommendationProvider:
    def __init__(
        self,
        inner: RecommendationProvider,
        policy: AiPolicy = AiPolicy(),
        *,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.policy = policy
        self._clock = clock
        self._sleep = sleep

    def scoped(self, budget: AiBudget) -> RecommendationProvider:
        return _ScopedExplainer(self, budget)

    async def explain(self, request: Request, limits: AiLimits) -> AiAnswer:
        """One call outside any run budget (tests, a future interactive use)."""
        return await self.explain_within(request, AiBudget.unlimited())

    async def explain_within(self, request: Request, budget: AiBudget) -> AiAnswer:
        limits = self.policy.limits
        size = request_size(request)
        if size > limits.max_request_chars:
            raise AiUnavailable(
                AiErrorCode.BUDGET, f"Запрос больше лимита ({size} > {limits.max_request_chars})."
            )
        attempt = 0
        while True:
            if not budget.take_request(self._clock()):
                raise AiUnavailable(AiErrorCode.BUDGET, BUDGET_MESSAGE)
            try:
                answer = await asyncio.wait_for(
                    self.inner.explain(request, limits), timeout=limits.timeout_seconds
                )
            except TimeoutError:
                error = AiUnavailable(AiErrorCode.TIMEOUT, "Модель не ответила в срок.")
            except AiUnavailable as failure:
                error = failure
            else:
                budget.charge(answer)
                return answer
            if error.code not in RETRIABLE or attempt >= self.policy.max_retries:
                raise error
            attempt += 1
            logger.info("ai_retry provider=%s code=%s attempt=%s", self.name, error.code, attempt)
            await self._sleep(self.policy.backoff_seconds * attempt)


class _ScopedExplainer:
    def __init__(self, guard: GuardedRecommendationProvider, budget: AiBudget) -> None:
        self._guard = guard
        self._budget = budget
        self.name = guard.name
        self.model = guard.model

    @property
    def budget(self) -> AiBudget:
        return self._budget

    async def explain(self, request: Request, limits: AiLimits) -> AiAnswer:
        return await self._guard.explain_within(request, self._budget)
