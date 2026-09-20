"""A stand-in for the model (S5-01): answers from the request itself, no network.

Until a real provider is chosen, everything above the provider boundary — request
building, review, storage, the report — is exercised against this stub. Its default
answer is built only from words already in the request (the priority label and the
rules' next step), so it passes the S5-06 review; a test can script any text, a delay or
a failure instead. Nothing is logged: the request holds the customer's data.
"""

import asyncio
import json
from collections.abc import Callable

from claims_assistant.application.recommendation import (
    AiAnswer,
    AiErrorCode,
    AiLimits,
    AiUnavailable,
)
from claims_assistant.domain.ai_context import Request

PROVIDER_NAME = "stub"
MODEL_NAME = "stub-1"
_PRIORITY_LABELS = {
    "critical": "критичный",
    "high": "высокий",
    "medium": "средний",
    "low": "низкий",
    "unknown": "недостаточно данных",
}


def default_answer(request: Request) -> str:
    """A valid answer in the S5-06 schema made of the request's own words only."""
    context = request.context
    priority = context.get("priority", "unknown")
    label = _PRIORITY_LABELS.get(priority, priority)
    grounds = []
    for signal in context.get("signals", []):
        for fact_id in signal.get("fact_ids", ()):
            if fact_id not in grounds:
                grounds.append(fact_id)
    if priority == "unknown":
        explanation = (
            f"По правилам данных для оценки недостаточно. {context.get('next_step', '')}".strip()
        )
    else:
        explanation = (
            f"По правилам приоритет {label}: сработали сигналы правил, основания перечислены. "
            f"{context.get('next_step', '')}".strip()
        )
    return json.dumps(
        {"explanation": explanation, "promises": [], "grounds": grounds}, ensure_ascii=False
    )


class StubRecommendationProvider:
    """Scriptable: ``script`` turns a request into the answer text, ``delay`` simulates
    latency, ``failure`` makes every call raise that AiUnavailable."""

    name = PROVIDER_NAME
    model = MODEL_NAME

    def __init__(
        self,
        script: Callable[[Request], str] = default_answer,
        *,
        delay_seconds: float = 0.0,
        failure: AiUnavailable | None = None,
    ) -> None:
        self._script = script
        self._delay = delay_seconds
        self._failure = failure
        self.requests: list[Request] = []  # what was asked, for tests

    async def explain(self, request: Request, limits: AiLimits) -> AiAnswer:
        self.requests.append(request)
        if self._delay > limits.timeout_seconds:
            raise AiUnavailable(AiErrorCode.TIMEOUT, "Модель не ответила в срок.")
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._failure is not None:
            raise self._failure
        text = self._script(request)
        # Rough token counts so budgets and logs have something to add up.
        input_tokens = (len(request.instruction) + len(json.dumps(request.context))) // 4
        return AiAnswer(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=max(1, len(text) // 4),
            latency_seconds=self._delay,
        )
