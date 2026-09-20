"""S3-04: Checko EFRSB (bankruptcy messages) section — paginated and source-linked.

CONFIRMED against the live API on 20.09.2026 (free tariff). Request:
``POST /v2/bankruptcy-messages`` with ``{"key", "inn", "page"}``. Response:

    {"meta": {"status": "ok", "today_request_count": int, "balance": float},
     "company": {...},
     "data": {"ЗапВсего": int, "СтрВсего": int, "СтрТекущ": int,
              "Записи": [{"GUID": str, "URL": str, "Дата": "YYYY-MM-DD",
                          "Тип": str, "ТипНаим": str, "НомерДела": str,
                          "Суд": str, "РешенСуда": str}, ...]}}

``Тип`` is the source's machine code (``ArbitralDecree``…), ``ТипНаим`` its Russian name;
the record is identified by ``GUID`` and ``URL`` points at its Fedresurs page. The earlier
assumption of a ``Номер`` field was wrong and is kept only as a fallback.

Event *types* are not classified here: scoring.md treats an unverified type as "requires
manual review", so we surface every message as a dated ``BANKRUPTCY_EVENT`` fact linked to
its source and never infer a confirmed procedure from raw text.
"""

import json
import re
from datetime import date, datetime
from typing import Protocol

import aiohttp

from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    Evidence,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    Section,
)
from claims_assistant.infrastructure.checko.errors import (
    ApiRejected,
    InvalidResponse,
    NotFound,
    is_not_found,
)

__all__ = ["read_page", "project_bankruptcy", "AiohttpBankruptcyTransport", "BankruptcyTransport"]

SOURCE = "checko-efrsb-v2"
BANKRUPTCY_URL = "https://api.checko.ru/v2/bankruptcy-messages"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# One record too far past the sample is dropped; a stable ceiling keeps memory bounded even
# if the envelope's counters are wrong.
MAX_RECORDS = 2000


class BankruptcyTransport(Protocol):
    async def request(
        self, key: str, inn: str, page: int, timeout: float
    ) -> tuple[int, object]: ...


class AiohttpBankruptcyTransport:
    async def request(self, key: str, inn: str, page: int, timeout: float) -> tuple[int, object]:
        # One session per page; key stays in the body, redirects and env proxies disabled.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                BANKRUPTCY_URL,
                json={"key": key, "inn": inn, "page": page},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return response.status, None
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise InvalidResponse() from None
                try:
                    return response.status, json.loads(body)
                except (ValueError, UnicodeError):
                    raise InvalidResponse() from None


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _text(value: object, limit: int = 500) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _url(value: object) -> str | None:
    """Only the source's own https link is kept; anything else is dropped silently."""
    text = _text(value, 1000)
    return text if text and text.startswith("https://") else None


def _label(record: dict) -> str:
    """What the user reads: the Russian name of the type, with the case number if given.

    The machine code (``Тип``) is not shown and is not classified here — scoring.md wants
    every message reviewed until the dictionary of types is agreed with the specialist.
    """
    if not isinstance(record, dict):
        return "Сообщение ЕФРСБ"
    label = _text(record.get("ТипНаим")) or _text(record.get("Тип")) or "Сообщение ЕФРСБ"
    case = _text(record.get("НомерДела"), 100)
    return f"{label}, дело {case}"[:500] if case else label[:500]


def read_page(payload: object, inn: str) -> tuple[list[dict], int, int]:
    """Validate one page envelope; return (records, total_pages, current_page)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise InvalidResponse()
    status = payload["meta"].get("status")
    if status == "error":
        raise ApiRejected()
    if status != "ok":
        raise InvalidResponse()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidResponse()
    if not data and is_not_found(payload["meta"]):
        raise NotFound()
    records = data.get("Записи")
    total_pages = data.get("СтрВсего")
    current_page = data.get("СтрТекущ")
    if (
        not isinstance(records, list)
        or type(total_pages) is not int
        or type(current_page) is not int
    ):
        raise InvalidResponse()
    # An empty result may come as zero pages; accept it only when the envelope agrees with
    # itself, otherwise a clean company would turn "unavailable" instead of "no messages".
    empty = total_pages == 0 and not records and data.get("ЗапВсего") == 0
    if not empty and (total_pages < 1 or current_page < 1):
        raise InvalidResponse()
    return records, total_pages, current_page


def project_bankruptcy(
    inn: str,
    records: list[dict],
    fetched_at: datetime,
    *,
    complete: bool,
    unreadable: int,
) -> ExternalSnapshot:
    """Build the section snapshot from the aggregated records across fetched pages."""
    facts = []
    evidence = []
    for index, record in enumerate(records[:MAX_RECORDS]):
        observed_on = _iso_date(record.get("Дата")) if isinstance(record, dict) else None
        if observed_on is None:
            unreadable += 1
            continue
        label = _label(record)
        # The source's own identifier of the message, and the page a person can open.
        record_id = _text(record.get("GUID")) or _text(record.get("Номер"))
        item = Evidence(
            f"efrsb-{index}",
            SOURCE,
            record_id or f"{inn}:{index}",
            url=_url(record.get("URL")),
        )
        evidence.append(item)
        facts.append(
            Fact(
                f"efrsb-event-{index}",
                inn,
                FactKind.BANKRUPTCY_EVENT,
                label[:500],
                (item.id,),
                observed_on=observed_on,
            )
        )

    missing = []
    if not complete:
        missing.append("Выборка сообщений ЕФРСБ неполная; проверьте реестр вручную.")
    if unreadable:
        missing.append("Часть сообщений ЕФРСБ без распознанной даты не учтена.")
    if facts:
        # Types are not classified here; the meaning of each message needs manual review.
        missing.append("Значение сообщений ЕФРСБ требует проверки специалистом.")

    coverage = Coverage.COMPLETE if not missing else Coverage.PARTIAL
    return ExternalSnapshot(
        inn=inn,
        section=Section.BANKRUPTCY,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=FetchStatus.OK,
        coverage=coverage,
        facts=tuple(facts),
        evidence=tuple(evidence),
        missing=tuple(missing),
    )
