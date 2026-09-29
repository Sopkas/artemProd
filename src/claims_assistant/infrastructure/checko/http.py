"""Reading a Checko HTTP answer, shared by every section transport."""

import json

from claims_assistant.infrastructure.checko.errors import InvalidResponse

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


async def read_response(response) -> tuple[int, object]:
    """Return (HTTP status, parsed JSON) with the body capped at MAX_RESPONSE_BYTES.

    Other non-200 answers are returned without a body. A 403 keeps it: Checko answers
    an exhausted daily limit with 403 (seen live 27.09.2026), and only ``meta.message``
    tells it from a real refusal. An unreadable 403 body is simply dropped.
    """
    if response.status not in (200, 403):
        return response.status, None
    try:
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise InvalidResponse() from None
        try:
            return response.status, json.loads(body)
        except (ValueError, UnicodeError):
            raise InvalidResponse() from None
    except InvalidResponse:
        if response.status == 403:
            return response.status, None
        raise
