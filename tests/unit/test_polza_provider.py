"""S5-01: the polza.ai client — what it sends, what it makes of the answer, how it fails.

No network here: the transport is a stand-in that returns what a test tells it to. The
shapes below are the ones the live API actually returned on 23.09.2026 (the probe behind
docs/ai-provider.md), including the error body and the usage block with its price.
"""

import json
from decimal import Decimal

import pytest

from claims_assistant.application.recommendation import (
    AiErrorCode,
    AiLimits,
    AiUnavailable,
    request_explanation,
)
from claims_assistant.domain.ai_context import build_request
from claims_assistant.infrastructure.llm.polza import (
    ANSWER_SCHEMA,
    DEFAULT_MODEL,
    PolzaRecommendationProvider,
    answer_schema,
    known_ids,
)
from claims_assistant.infrastructure.llm.stub import default_answer
from tests.unit.test_ai_evals import INN, context

KEY = "pza_test_key"
LIMITS = AiLimits(timeout_seconds=7, max_output_tokens=500)
# The answer of the stand-in model: valid in the S5-06 schema and built from the request
# itself, so these tests are about the transport, not about what a model might write.
VALID = default_answer(build_request(context()))


def answer(content: str = VALID):
    return {
        "id": "gen_1",
        "model": "openai/gpt-4o-mini",
        "provider": "openrouter",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 99,
            "completion_tokens": 38,
            "total_tokens": 137,
            "cost_rub": 0.0044424,
        },
    }


_DEFAULT = object()


class Transport:
    """Stands in for the HTTP call; remembers what it was asked to send."""

    def __init__(self, status: int = 200, body: object = _DEFAULT, error: Exception | None = None):
        # `None` is a body in its own right here (the answer was not JSON), so the
        # default is a sentinel rather than None.
        self._status, self._body, self._error = (
            status,
            answer() if body is _DEFAULT else body,
            error,
        )
        self.calls: list[dict] = []
        self.timeouts: list[float] = []
        self.keys: list[str] = []

    async def request(self, key, url, payload, timeout):
        self.calls.append(payload)
        self.timeouts.append(timeout)
        self.keys.append(key)
        if self._error is not None:
            raise self._error
        return self._status, self._body


def provider(transport, **kwargs) -> PolzaRecommendationProvider:
    return PolzaRecommendationProvider(KEY, transport=transport, **kwargs)


async def test_the_request_carries_the_instruction_the_context_and_our_limits():
    transport = Transport()
    request = build_request(context())
    await provider(transport).explain(request, LIMITS)
    (payload,) = transport.calls
    assert payload["model"] == DEFAULT_MODEL
    assert payload["messages"][0] == {"role": "system", "content": request.instruction}
    assert json.loads(payload["messages"][1]["content"]) == request.context
    assert payload["max_tokens"] == LIMITS.max_output_tokens  # the answer is bounded by us
    assert payload["temperature"] == 0  # the same context must give the same explanation
    assert payload["response_format"]["json_schema"]["schema"] == answer_schema(request.context)
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert transport.timeouts == [LIMITS.timeout_seconds] and transport.keys == [KEY]


async def test_the_inn_does_not_leave_with_the_request():
    """The context already masks it (S5-05/#45); the adapter must not add it back."""
    transport = Transport()
    await provider(transport).explain(build_request(context()), LIMITS)
    assert INN not in json.dumps(transport.calls[0], ensure_ascii=False)


async def test_a_model_without_a_schema_is_asked_for_json_in_words():
    transport = Transport()
    await provider(transport, model="sber/gigachat-2", structured=False).explain(
        build_request(context()), LIMITS
    )
    (payload,) = transport.calls
    assert "response_format" not in payload  # the provider would refuse the parameter
    assert "JSON" in payload["messages"][0]["content"]
    assert payload["model"] == "sber/gigachat-2"


async def test_the_answer_keeps_the_text_the_counts_and_the_time():
    transport = Transport()
    result = await provider(transport).explain(build_request(context()), LIMITS)
    assert json.loads(result.text) == json.loads(VALID)
    assert result.provider == "polza" and result.model == "openai/gpt-4o-mini"
    assert (result.input_tokens, result.output_tokens) == (99, 38)
    assert result.latency_seconds >= 0


async def test_the_answer_reaches_the_review_as_an_explanation():
    """End to end over the boundary: what the provider returns is what S5-06 accepts."""
    outcome = await request_explanation(provider(Transport()), context(), LIMITS)
    assert outcome.status == "accepted"
    assert outcome.explanation.text == json.loads(VALID)["explanation"]


@pytest.mark.parametrize(
    "status, code",
    [
        (400, AiErrorCode.REFUSED),  # «Модель … не найдена»
        (401, AiErrorCode.REFUSED),  # «Неверные учётные данные авторизации»
        (403, AiErrorCode.REFUSED),
        (402, AiErrorCode.BUDGET),  # money at the provider ran out
        (408, AiErrorCode.TIMEOUT),
        (429, AiErrorCode.RATE_LIMITED),
        (500, AiErrorCode.UNAVAILABLE),
        (503, AiErrorCode.UNAVAILABLE),  # «нет доступных провайдеров»
    ],
)
async def test_the_providers_codes_become_ours(status, code):
    body = {"error": {"code": "BAD_REQUEST", "message": "что-то пошло не так"}}
    with pytest.raises(AiUnavailable) as failure:
        await provider(Transport(status, body)).explain(build_request(context()), LIMITS)
    assert failure.value.code is code
    assert "что-то пошло не так" not in failure.value.message  # never quoted back


async def test_a_dropped_connection_is_unavailable_and_says_nothing_about_the_address():
    transport = Transport(error=OSError("Cannot connect to host polza.ai:443 key=pza_secret"))
    with pytest.raises(AiUnavailable) as failure:
        await provider(transport).explain(build_request(context()), LIMITS)
    assert failure.value.code is AiErrorCode.UNAVAILABLE
    assert "polza.ai" not in failure.value.message and "pza_" not in failure.value.message


async def test_a_slow_call_is_a_timeout():
    with pytest.raises(AiUnavailable) as failure:
        await provider(Transport(error=TimeoutError())).explain(build_request(context()), LIMITS)
    assert failure.value.code is AiErrorCode.TIMEOUT


@pytest.mark.parametrize(
    "body",
    [
        None,  # the answer was not JSON at all
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {}}]},
    ],
)
async def test_an_answer_without_text_is_unavailable_not_an_empty_explanation(body):
    with pytest.raises(AiUnavailable) as failure:
        await provider(Transport(200, body)).explain(build_request(context()), LIMITS)
    assert failure.value.code is AiErrorCode.UNAVAILABLE


async def test_an_answer_cut_off_by_the_length_limit_is_not_passed_on():
    """«Не дали договорить» is not «ответила плохо»: the report must not blame the model."""
    body = answer('{"explanation": "Начало объяс')
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(AiUnavailable) as failure:
        await provider(Transport(200, body)).explain(build_request(context()), LIMITS)
    # Its own code, not «unavailable»: a lost connection is worth a retry, a cut-off
    # answer is not — the same request stops at the same length and is paid for again.
    assert failure.value.code is AiErrorCode.TRUNCATED
    assert "обрезан" in failure.value.message
    # The call was paid for all the same; what it cost travels with the failure.
    spent = failure.value.spent
    assert spent is not None
    assert (spent.input_tokens, spent.output_tokens) == (99, 38)
    assert spent.cost_rub == Decimal("0.0044424")


async def test_a_model_stopped_while_still_reasoning_is_cut_off_too():
    """A reasoning model can use the whole limit on its reasoning: the text is then empty,
    and that is still a cut-off, not an answer we failed to parse."""
    body = answer("")
    body["choices"][0]["finish_reason"] = "length"
    body["usage"] = {"prompt_tokens": 1200, "completion_tokens": 500, "cost_rub": 0.09}
    with pytest.raises(AiUnavailable) as failure:
        await provider(Transport(200, body)).explain(build_request(context()), LIMITS)
    assert failure.value.code is AiErrorCode.TRUNCATED
    assert failure.value.spent.cost_rub == Decimal("0.09")


async def test_a_failure_before_any_answer_costs_nothing():
    with pytest.raises(AiUnavailable) as failure:
        await provider(Transport(503, {"error": {}})).explain(build_request(context()), LIMITS)
    assert failure.value.spent is None


@pytest.mark.parametrize("usage", [{}, {"prompt_tokens": "99"}, {"prompt_tokens": -5}, None])
async def test_missing_token_counts_are_zero_and_never_guessed(usage):
    """The run budget (S5-03) may only be charged with numbers the provider gave us."""
    body = answer()
    body["usage"] = usage
    result = await provider(Transport(200, body)).explain(build_request(context()), LIMITS)
    assert (result.input_tokens, result.output_tokens) == (0, 0)


async def test_the_key_is_required_and_is_not_in_the_object_repr():
    with pytest.raises(ValueError):
        PolzaRecommendationProvider("")
    assert KEY not in repr(provider(Transport()))


async def test_the_schema_lets_the_model_cite_only_the_ids_of_this_context():
    """The first live answer cited a signal code as a ground and was rejected whole; the
    schema now leaves no such choice."""
    request = build_request(context())
    schema = answer_schema(request.context)
    allowed = schema["properties"]["grounds"]["items"]["enum"]
    assert allowed == known_ids(request.context) and allowed
    assert all(fact["id"] in allowed for fact in request.context["facts"])
    assert "revenue_drop_30" not in allowed  # a signal code is not a ground
    assert schema["properties"]["explanation"] == ANSWER_SCHEMA["properties"]["explanation"]


async def test_a_context_without_a_single_id_allows_no_grounds_at_all():
    empty = {"facts": [], "values": [], "comments": []}
    grounds = answer_schema(empty)["properties"]["grounds"]
    assert grounds["maxItems"] == 0 and "enum" not in grounds["items"]
