"""The recommendation provider boundary (S5-01).

A provider sends one counterparty's request (S5-05) to a model and returns the raw
answer with what it cost; it never interprets the answer — ``domain/ai_review`` does that
here, in the application layer, so every provider is judged by the same rules. Expected
failures (the model is down, slow, rate limited, refused the request, or the budget is
gone) are ``AiUnavailable`` with a stable code; programming errors propagate.

No real provider is wired until the customer confirms which one, what data may leave
the premises and what it may cost (roadmap S5-01). Until then the stub provider stands in
for the model, and the pipeline stores nothing (S5-02) and shows nothing (S5-04).
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from claims_assistant.domain.ai_context import RecommendationContext, Request, build_request
from claims_assistant.domain.ai_review import Explanation, Rejected, review_answer

logger = logging.getLogger(__name__)


class AiErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"  # network, HTTP 5xx, malformed transport answer
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    REFUSED = "refused"  # the provider declined the request (policy, auth, bad request)
    BUDGET = "budget"  # our own limit: request too large or spend exhausted


class AiUnavailable(RuntimeError):
    """An expected provider failure; ``message`` is safe to log and show."""

    def __init__(self, code: AiErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AiLimits:
    """Limits of one request; the full budget policy is S5-03."""

    timeout_seconds: float = 30.0
    max_request_chars: int = 40_000  # the S5-07 bound: 20 comments × 500 chars fit easily
    max_output_tokens: int = 800  # 2–4 sentences of explanation plus the JSON around it

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_request_chars <= 0 or self.max_output_tokens <= 0:
            raise ValueError("AI limits must be positive")


@dataclass(frozen=True, slots=True)
class AiAnswer:
    """The model's answer as received, plus what it cost; interpretation is not here."""

    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_seconds: float


@runtime_checkable
class RecommendationProvider(Protocol):
    name: str
    model: str

    async def explain(self, request: Request, limits: AiLimits) -> AiAnswer:
        """One request for one counterparty; AiUnavailable on an expected failure.

        Adapters enforce ``limits`` (timeout, output size) and never log the request
        body or the answer: both may hold the customer's data.
        """
        ...


@dataclass(frozen=True, slots=True)
class ExplanationOutcome:
    """What one attempt produced: an accepted explanation, a rejected answer or no answer."""

    provider: str
    model: str
    versions: str  # context/instruction/schema versions the answer was produced with
    answer: AiAnswer | None = None
    explanation: Explanation | None = None
    rejected: Rejected | None = None
    error: AiUnavailable | None = None

    @property
    def status(self) -> str:
        if self.explanation is not None:
            return "accepted"
        if self.rejected is not None:
            return f"rejected:{self.rejected.code}"
        return f"unavailable:{self.error.code}" if self.error else "unavailable"


def request_size(request: Request) -> int:
    return len(request.instruction) + len(json.dumps(request.context, ensure_ascii=False))


async def request_explanation(
    provider: RecommendationProvider,
    context: RecommendationContext,
    limits: AiLimits = AiLimits(),
) -> ExplanationOutcome:
    """Build the request, ask the provider, review the answer; never raises on the
    expected failures — the outcome says what happened, and the caller decides what
    the report shows (S5-04)."""
    from claims_assistant.domain.ai_review import ANSWER_SCHEMA_VERSION

    request = build_request(context)
    # Every version the answer depends on; a change of any of them is a new step key.
    versions = (
        f"{provider.name}:{provider.model}:i{request.instruction_version}"
        f":c{request.context_version}:s{ANSWER_SCHEMA_VERSION}:r{context.rules_version}"
    )
    size = request_size(request)
    if size > limits.max_request_chars:
        error = AiUnavailable(
            AiErrorCode.BUDGET, f"Запрос больше лимита ({size} > {limits.max_request_chars})."
        )
        logger.warning("ai_request_too_large size=%s limit=%s", size, limits.max_request_chars)
        return ExplanationOutcome(provider.name, provider.model, versions, error=error)
    started = time.monotonic()
    try:
        answer = await provider.explain(request, limits)
    except AiUnavailable as error:
        logger.warning(
            "ai_unavailable provider=%s code=%s seconds=%.2f",
            provider.name,
            error.code,
            time.monotonic() - started,
        )
        return ExplanationOutcome(provider.name, provider.model, versions, error=error)
    reviewed = review_answer(answer.text, context)
    if isinstance(reviewed, Rejected):
        logger.info(
            "ai_answer_rejected provider=%s code=%s tokens=%s+%s",
            provider.name,
            reviewed.code,
            answer.input_tokens,
            answer.output_tokens,
        )
        return ExplanationOutcome(
            provider.name, provider.model, versions, answer=answer, rejected=reviewed
        )
    logger.info(
        "ai_answer_accepted provider=%s tokens=%s+%s seconds=%.2f",
        provider.name,
        answer.input_tokens,
        answer.output_tokens,
        answer.latency_seconds,
    )
    return ExplanationOutcome(
        provider.name, provider.model, versions, answer=answer, explanation=reviewed
    )
