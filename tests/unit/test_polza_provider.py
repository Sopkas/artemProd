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
    ModelParameters,
    PolzaRecommendationProvider,
    answer_schema,
    known_ids,
    parameters_from_catalog,
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


# Catalogue entries as polza returned them on 26.09.2026 (GET /api/v1/models/<id>), with
# the long descriptions and the per-provider list left out.
GPT_41_MINI = {
    "id": "openai/gpt-4.1-mini",
    "name": "OpenAI: GPT-4.1 Mini",
    "type": "chat",
    "created": 1769617411,
    "top_provider": {
        "name": "azure",
        "context_length": 1047576,
        "max_completion_tokens": 942818,
        "pricing": {
            "prompt_per_million": "47.52160000",
            "completion_per_million": "190.08640000",
            "currency": "RUB",
        },
        "supported_parameters": [
            "max_completion_tokens",
            "seed",
            "response_format",
            "structured_outputs",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "max_tokens",
        ],
        "default_parameters": {"temperature": None, "top_p": None, "frequency_penalty": None},
    },
}
GPT_5_MINI = {
    "id": "openai/gpt-5-mini",
    "name": "OpenAI: GPT-5 Mini",
    "type": "chat",
    "created": 1769617413,
    "top_provider": {
        "name": "openai/flex",
        "context_length": 400000,
        "max_completion_tokens": 128000,
        "pricing": {
            "prompt_per_million": "14.85050000",
            "completion_per_million": "118.80400000",
            "currency": "RUB",
        },
        "supported_parameters": [
            "reasoning",
            "include_reasoning",
            "structured_outputs",
            "response_format",
            "seed",
            "max_tokens",
            "tools",
            "tool_choice",
            "reasoning_effort",
        ],
        "default_parameters": {"temperature": None, "top_p": None, "frequency_penalty": None},
    },
}
GEMINI_25_FLASH = {
    "id": "google/gemini-2.5-flash",
    "name": "Google: Gemini 2.5 Flash",
    "type": "chat",
    "created": 1769617391,
    "top_provider": {
        "name": "mie",
        "context_length": 1048576,
        "max_completion_tokens": None,
        "pricing": {
            "prompt_per_million": "20.62098000",
            "completion_per_million": "171.84150000",
            "currency": "RUB",
        },
        "supported_parameters": ["max_tokens"],
        "default_parameters": None,
    },
}

QWEN_36_FLASH = {
    "id": "qwen/qwen3.6-flash",
    "name": "Qwen: Qwen3.6 Flash",
    "type": "chat",
    "created": 1777307727,
    "top_provider": {
        "name": "alibaba",
        "context_length": 1000000,
        "max_completion_tokens": 65536,
        "pricing": {
            "prompt_per_million": "22.27575000",
            "completion_per_million": "133.65450000",
            "currency": "RUB",
        },
        # «reasoning» without «reasoning_effort», like 96 of the 179 reasoning models.
        "supported_parameters": [
            "reasoning",
            "include_reasoning",
            "max_tokens",
            "temperature",
            "top_p",
            "seed",
            "presence_penalty",
            "response_format",
            "tools",
            "tool_choice",
            "structured_outputs",
            "logprobs",
            "top_logprobs",
            "top_k",
            "frequency_penalty",
            "stop",
        ],
        "default_parameters": {"temperature": None, "top_p": None, "frequency_penalty": None},
    },
}

_DEFAULT = object()


class Transport:
    """Stands in for the HTTP calls; remembers what it was asked to send.

    ``catalog`` is what the model's catalogue entry answers: an entry (HTTP 200), a
    ``(status, body)`` pair, or an exception to raise.
    """

    def __init__(
        self,
        status: int = 200,
        body: object = _DEFAULT,
        error: Exception | None = None,
        catalog: object = GPT_41_MINI,
    ):
        # `None` is a body in its own right here (the answer was not JSON), so the
        # default is a sentinel rather than None.
        self._status, self._body, self._error = (
            status,
            answer() if body is _DEFAULT else body,
            error,
        )
        self._catalog = catalog
        self.calls: list[dict] = []
        self.timeouts: list[float] = []
        self.keys: list[str] = []
        self.fetched: list[str] = []

    async def request(self, key, url, payload, timeout):
        self.calls.append(payload)
        self.timeouts.append(timeout)
        self.keys.append(key)
        if self._error is not None:
            raise self._error
        return self._status, self._body

    async def fetch(self, url, timeout):
        self.fetched.append(url)
        if isinstance(self._catalog, Exception):
            raise self._catalog
        if isinstance(self._catalog, tuple):
            return self._catalog
        return 200, self._catalog


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


# --- what each model accepts: the provider's catalogue (S5-07) ---------------------------


@pytest.mark.parametrize(
    "entry, expected",
    [
        (GPT_41_MINI, ModelParameters(temperature=True, response_format=True, reasoning=False)),
        (GPT_5_MINI, ModelParameters(temperature=False, response_format=True, reasoning=True)),
        (QWEN_36_FLASH, ModelParameters(temperature=True, response_format=True, reasoning=True)),
        (
            GEMINI_25_FLASH,
            ModelParameters(temperature=False, response_format=False, reasoning=False),
        ),
    ],
)
def test_what_a_model_accepts_is_read_from_its_catalogue_entry(entry, expected):
    assert parameters_from_catalog(entry) == expected


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"top_provider": None},
        {"top_provider": {"supported_parameters": "max_tokens"}},
        {"error": {"code": "NOT_FOUND", "message": "Модель не найдена"}},
    ],
)
def test_an_entry_we_cannot_read_says_nothing_about_the_model(entry):
    assert parameters_from_catalog(entry) is None


async def test_a_reasoning_model_gets_no_temperature_and_a_low_reasoning_effort():
    """Ninety of the catalogue's chat models answer 400 to a temperature, and a reasoning
    model left at its own effort can spend our whole output limit before it answers."""
    transport = Transport(catalog=GPT_5_MINI)
    await provider(transport, model="openai/gpt-5-mini").explain(build_request(context()), LIMITS)
    (payload,) = transport.calls
    assert "temperature" not in payload
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_tokens"] == LIMITS.max_output_tokens
    assert payload["response_format"]["json_schema"]["strict"] is True


async def test_a_model_without_reasoning_gets_no_reasoning_block():
    transport = Transport(catalog=GPT_41_MINI)
    await provider(transport).explain(build_request(context()), LIMITS)
    assert "reasoning" not in transport.calls[0] and transport.calls[0]["temperature"] == 0


async def test_a_model_that_takes_no_schema_is_asked_for_json_in_words_whatever_the_setting():
    transport = Transport(catalog=GEMINI_25_FLASH)
    await provider(transport, model="google/gemini-2.5-flash").explain(
        build_request(context()), LIMITS
    )
    (payload,) = transport.calls
    assert "response_format" not in payload
    assert "JSON" in payload["messages"][0]["content"]


async def test_the_catalogue_is_asked_once_per_model():
    transport = Transport()
    client = provider(transport)
    for _ in range(2):
        await client.explain(build_request(context()), LIMITS)
    assert transport.fetched == [f"https://polza.ai/api/v1/models/{DEFAULT_MODEL}"]
    assert [call["temperature"] for call in transport.calls] == [0, 0]


@pytest.mark.parametrize(
    "catalog",
    [OSError("Cannot connect"), (404, {"error": {"code": "NOT_FOUND"}}), (200, None)],
)
async def test_without_the_catalogue_only_what_every_model_accepts_is_sent(catalog):
    """The explanation is still asked for; the parameters some models refuse are left out
    until the catalogue answers. A failed catalogue is not asked again at once: at up to
    15 s a call, a check of 50 companies would spend 12 minutes on it (review B on #63)."""
    transport = Transport(catalog=catalog)
    client = provider(transport, clock=lambda: 1000.0)
    for _ in range(2):
        await client.explain(build_request(context()), LIMITS)
    assert len(transport.calls) == 2 and len(transport.fetched) == 1
    for payload in transport.calls:
        assert "temperature" not in payload and "reasoning" not in payload
        assert payload["max_tokens"] == LIMITS.max_output_tokens
        assert "response_format" in payload  # the setting decides while nothing is known


async def test_a_failed_catalogue_is_asked_again_five_minutes_later():
    now = [1000.0]
    transport = Transport(catalog=OSError("Cannot connect"))
    client = provider(transport, clock=lambda: now[0])
    await client.explain(build_request(context()), LIMITS)
    now[0] += 299
    await client.explain(build_request(context()), LIMITS)
    assert len(transport.fetched) == 1
    now[0] += 2  # 301 s after the failure
    await client.explain(build_request(context()), LIMITS)
    assert len(transport.fetched) == 2


async def test_parameters_given_up_front_are_used_without_asking_the_catalogue():
    """The bench reads the catalogue itself, once, before it spends any money."""
    transport = Transport()
    known = parameters_from_catalog(GPT_5_MINI)
    await provider(transport, parameters=known).explain(build_request(context()), LIMITS)
    assert transport.fetched == []
    assert "temperature" not in transport.calls[0]


async def test_the_reasoning_effort_can_be_chosen():
    transport = Transport(catalog=GPT_5_MINI)
    await provider(transport, reasoning_effort="minimal").explain(build_request(context()), LIMITS)
    assert transport.calls[0]["reasoning"] == {"effort": "minimal"}


async def test_a_refused_request_hands_its_body_to_whoever_asked_for_it():
    """For choosing a model the provider's own words are what tells «модель не подошла»
    from «мы послали не то»; they go to the caller's hook, never to the log or the user."""
    seen = []
    body = {"error": {"code": "BAD_REQUEST", "message": "Unsupported parameter: temperature"}}
    client = provider(
        Transport(400, body), on_failure=lambda status, got: seen.append((status, got))
    )
    with pytest.raises(AiUnavailable) as failure:
        await client.explain(build_request(context()), LIMITS)
    assert seen == [(400, body)]
    assert "temperature" not in failure.value.message
