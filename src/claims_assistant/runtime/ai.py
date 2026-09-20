"""Which recommendation provider the service runs with (S5-01).

``AI_PROVIDER=off`` gives no provider: the pipeline does not ask for explanations and the
report shows the rules' recommendation (S5-04). ``stub`` runs the stand-in model for
local checks and demos. A real provider appears here only after the customer confirms
the provider, the data that may be sent and the spending limit.
"""

from claims_assistant.application.recommendation import RecommendationProvider
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider

from .settings import Settings


def build_recommendation_provider(settings: Settings) -> RecommendationProvider | None:
    if settings.ai_provider == "stub":
        return StubRecommendationProvider()
    return None
