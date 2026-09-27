"""The real recommendation provider (S5-01): the polza.ai aggregator.

The provider was chosen on 23.09.2026; the reconnaissance behind this adapter is in
``docs/ai-provider.md``. In short: the API is the OpenAI one — ``POST /chat/completions``
at ``https://polza.ai/api/v1`` with ``Authorization: Bearer <ключ>`` — the answer carries
the token counts and the price of the call in roubles, and models that support it take a
strict JSON schema, which is exactly the shape ``domain/ai_review`` checks.

What this adapter does and does not do:

- it sends one counterparty's request and returns the raw answer with its cost; the
  answer is judged in the application layer, by the same rules for every provider;
- expected failures become ``AiUnavailable`` with our own codes, so the guard (S5-03)
  can retry the transient ones and the report can say what happened (S5-04);
- neither the request nor the answer is ever logged: both hold the customer's data. What
  is logged is the arithmetic — tokens, roubles, seconds — because the spend has to be
  visible to whoever pays for it.

Models without ``response_format`` (the Russian-hosted ones, see the doc) are supported
by asking for JSON in the instruction instead: set ``AI_RESPONSE_FORMAT=none``. Then the
shape is not enforced by the provider, only checked by S5-06 — a rejected answer costs a
request and falls back to the rules' explanation.
"""

import asyncio
import json
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import aiohttp

from claims_assistant.application.recommendation import (
    AiAnswer,
    AiErrorCode,
    AiLimits,
    AiUnavailable,
)
from claims_assistant.domain.ai_context import Request
from claims_assistant.domain.ai_review import MAX_PROMISES

logger = logging.getLogger(__name__)

PROVIDER_NAME = "polza"
DEFAULT_MODEL = "openai/gpt-4.1-mini"
DEFAULT_URL = "https://polza.ai/api/v1/chat/completions"
MAX_RESPONSE_BYTES = 1024 * 1024  # an explanation is a few hundred bytes; this is slack

# The answer shape of S5-06, as a JSON schema the provider can enforce. «quote» is left
# out on purpose: the review accepts a promise without it, and a required quote would
# invite the model to invent one.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "promises": {
            "type": "array",
            "maxItems": MAX_PROMISES,
            "items": {
                "type": "object",
                "properties": {
                    "interaction_id": {"type": "string"},
                    "due_on": {"type": "string"},
                },
                "required": ["interaction_id", "due_on"],
                "additionalProperties": False,
            },
        },
        "grounds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["explanation", "promises", "grounds"],
    "additionalProperties": False,
}


def known_ids(context: dict[str, Any]) -> list[str]:
    """The ids the review (S5-06) accepts as grounds: values, facts and comments.

    Kept in the same order the context lists them, so the schema of two identical
    requests is identical and the stored answer stays comparable.
    """
    ids: list[str] = []
    for key, field in (("values", "id"), ("facts", "id"), ("comments", "interaction_id")):
        for item in context.get(key) or ():
            value = item.get(field) if isinstance(item, dict) else None
            if isinstance(value, str) and value not in ids:
                ids.append(value)
    return ids


def answer_schema(context: dict[str, Any]) -> dict[str, Any]:
    """The schema for one request, with the grounds bound to this context's own ids.

    The first live answer (23.09.2026) cited ``revenue_drop_30`` — the code of a signal,
    not an id of a fact — and the review rejected the whole explanation for it. The
    provider can prevent that instead: an enum of the ids that exist here makes an
    invented ground impossible rather than merely caught.
    """
    schema = json.loads(json.dumps(ANSWER_SCHEMA))  # a copy: the constant stays untouched
    ids = known_ids(context)
    schema["properties"]["grounds"] = (
        {"type": "array", "items": {"type": "string", "enum": ids}}
        if ids
        else {"type": "array", "maxItems": 0, "items": {"type": "string"}}
    )
    return schema


# Asked for in words when the model cannot be given a schema.
_JSON_ONLY = (
    "Ответ — один объект JSON без пояснений вокруг него, с полями "
    '"explanation" (строка), "promises" (список) и "grounds" (список строк).'
)

_MESSAGES = {
    AiErrorCode.UNAVAILABLE: "Провайдер ИИ недоступен.",
    AiErrorCode.TIMEOUT: "Модель не ответила в срок.",
    AiErrorCode.RATE_LIMITED: "Провайдер ИИ ограничил число запросов.",
    AiErrorCode.REFUSED: "Провайдер ИИ отклонил запрос.",
    AiErrorCode.BUDGET: "Средства на счёте провайдера ИИ закончились.",
}


class ChatTransport(Protocol):
    """One HTTP call; the adapter knows nothing about the library behind it."""

    async def request(
        self, key: str, url: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, object]: ...


class AiohttpChatTransport:
    """Session per call, no redirects, no environment proxies, bounded answer size."""

    async def request(
        self, key: str, url: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, object]:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                url, json=payload, headers=headers, allow_redirects=False
            ) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise _TooLarge()
                try:
                    return response.status, json.loads(body)
                except (ValueError, UnicodeError):
                    return response.status, None


class _TooLarge(Exception):
    """The answer is larger than any explanation could be; treated as unavailable."""


def _failure(http_status: int) -> AiErrorCode:
    """The provider's own codes (docs/ai-provider.md) mapped onto ours."""
    if http_status in (401, 403, 400, 404):
        return AiErrorCode.REFUSED  # key, policy or a request the provider will not take
    if http_status == 402:
        return AiErrorCode.BUDGET  # money at the provider ran out: not a retry
    if http_status == 408:
        return AiErrorCode.TIMEOUT
    if http_status == 429:
        return AiErrorCode.RATE_LIMITED
    return AiErrorCode.UNAVAILABLE  # 5xx, 502/503 «нет провайдеров», anything unknown


class PolzaRecommendationProvider:
    """``RecommendationProvider`` over the polza.ai chat API."""

    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        url: str = DEFAULT_URL,
        transport: ChatTransport | None = None,
        structured: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("polza.ai API key is required")
        self.model = model
        self._key = api_key
        self._url = url
        self._transport = transport or AiohttpChatTransport()
        self._structured = structured

    def _payload(self, request: Request, limits: AiLimits) -> dict[str, Any]:
        instruction = (
            request.instruction if self._structured else f"{request.instruction}\n{_JSON_ONLY}"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(request.context, ensure_ascii=False)},
            ],
            "max_tokens": limits.max_output_tokens,
            "temperature": 0,  # the same context must give the same explanation
        }
        if self._structured:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "explanation",
                    "strict": True,
                    "schema": answer_schema(request.context),
                },
            }
        return payload

    async def explain(self, request: Request, limits: AiLimits) -> AiAnswer:
        payload = self._payload(request, limits)
        started = time.monotonic()
        try:
            status, body = await self._transport.request(
                self._key, self._url, payload, limits.timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise AiUnavailable(AiErrorCode.TIMEOUT, _MESSAGES[AiErrorCode.TIMEOUT]) from None
        except _TooLarge:
            raise AiUnavailable(
                AiErrorCode.UNAVAILABLE, "Ответ провайдера ИИ больше допустимого."
            ) from None
        except (aiohttp.ClientError, OSError):
            # The error text may hold the address and the key: it is not carried over.
            raise AiUnavailable(
                AiErrorCode.UNAVAILABLE, _MESSAGES[AiErrorCode.UNAVAILABLE]
            ) from None
        latency = time.monotonic() - started
        if status != 200:
            code = _failure(status)
            logger.info(
                "ai_call provider=%s model=%s http=%s code=%s", self.name, self.model, status, code
            )
            raise AiUnavailable(code, _MESSAGES[code])
        return self._answer(body, latency)

    def _answer(self, body: object, latency: float) -> AiAnswer:
        text = _content(body)
        if text is None:
            raise AiUnavailable(AiErrorCode.UNAVAILABLE, "Ответ провайдера ИИ не разобран.")
        if _truncated(body):
            # A cut-off answer is not a bad answer: it is no answer, and saying so keeps
            # «модель ответила плохо» and «мы не дали ей договорить» apart in the report.
            raise AiUnavailable(AiErrorCode.UNAVAILABLE, "Ответ модели обрезан лимитом длины.")
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        logger.info(
            "ai_call provider=%s model=%s in=%s out=%s cost_rub=%s seconds=%.1f",
            self.name,
            self.model,
            _count(usage, "prompt_tokens"),
            _count(usage, "completion_tokens"),
            usage.get("cost_rub"),
            latency,
        )
        return AiAnswer(
            text=text,
            provider=self.name,
            model=str(body.get("model") or self.model) if isinstance(body, dict) else self.model,
            input_tokens=_count(usage, "prompt_tokens"),
            output_tokens=_count(usage, "completion_tokens"),
            latency_seconds=latency,
            cost_rub=_price(usage),
        )


def _choice(body: object) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]


def _content(body: object) -> str | None:
    choice = _choice(body)
    message = choice.get("message") if choice is not None else None
    text = message.get("content") if isinstance(message, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def _truncated(body: object) -> bool:
    choice = _choice(body)
    return choice is not None and choice.get("finish_reason") == "length"


def _price(usage: dict[str, Any]) -> Decimal | None:
    """What this call cost, as the provider reported it; None when it did not.

    The value comes as a JSON number, and money must not be added up in binary floats:
    it is converted through its own text so 0.0044424 stays 0.0044424.
    """
    value = usage.get("cost_rub")
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _count(usage: dict[str, Any], key: str) -> int:
    """A token count as the provider reports it; anything else counts as zero, never as
    a guess — the budget (S5-03) may only be charged with numbers we were given."""
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else 0
