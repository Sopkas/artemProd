"""Which recommendation provider the service runs with (S5-01).

``AI_PROVIDER=off`` gives no provider: the pipeline does not ask for explanations and the
report shows the rules' recommendation (S5-04). ``stub`` runs the stand-in model for
local checks and demos. A real provider appears here only after the customer confirms
the provider, the data that may be sent and the spending limit.
"""

from claims_assistant.application.ai_guard import (
    AiPolicy,
    AiRunLimits,
    GuardedRecommendationProvider,
)
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider

from .settings import Settings


def build_recommendation_provider(settings: Settings) -> GuardedRecommendationProvider | None:
    """The configured provider behind the S5-03 guard (timeouts, one retry, sizes)."""
    if settings.ai_provider == "stub":
        inner = StubRecommendationProvider()
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
    )
