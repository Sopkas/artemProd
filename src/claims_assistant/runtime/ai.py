"""Which recommendation provider the service runs with (S5-01).

``AI_PROVIDER=off`` gives no provider: the pipeline does not ask for explanations and the
report shows the rules' recommendation (S5-04). ``stub`` runs the stand-in model for
local checks and demos. ``polza`` is the provider chosen on 23.09.2026 — the aggregator
polza.ai, with its key in ``AI_API_KEY`` and the model in ``AI_MODEL``
(docs/ai-provider.md). What may be sent is still a separate decision: comments stay off
until the customer agrees (``AI_SEND_COMMENTS``), and the INN never leaves at all.
"""

from claims_assistant.application.ai_guard import (
    AiPolicy,
    AiRunLimits,
    GuardedRecommendationProvider,
)
from claims_assistant.application.recommendation import RecommendationProvider
from claims_assistant.infrastructure.llm.polza import (
    DEFAULT_MODEL,
    PolzaRecommendationProvider,
)
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider

from .settings import Settings


def build_recommendation_provider(settings: Settings) -> GuardedRecommendationProvider | None:
    """The configured provider behind the S5-03 guard (timeouts, one retry, sizes)."""
    if settings.ai_provider == "stub":
        inner: RecommendationProvider = StubRecommendationProvider()
    elif settings.ai_provider == "polza":
        inner = PolzaRecommendationProvider(
            settings.ai_api_key,
            model=settings.ai_model or DEFAULT_MODEL,
            structured=settings.ai_structured,
        )
    else:
        return None
    limits = settings.ai
    return GuardedRecommendationProvider(
        inner,
        AiPolicy(
            timeout_seconds=limits.timeout_seconds,
            max_retries=limits.max_retries,
            max_request_chars=limits.max_request_chars,
            max_output_tokens=limits.max_output_tokens,
        ),
    )


def run_limits(settings: Settings) -> AiRunLimits:
    return AiRunLimits(
        max_requests=settings.ai.run_request_limit,
        max_tokens=settings.ai.run_token_limit,
        max_seconds=settings.ai.run_time_limit_seconds,
        max_rub=settings.ai.run_rub_limit,
    )
