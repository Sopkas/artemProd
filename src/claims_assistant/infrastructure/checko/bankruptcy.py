"""S3-04: Checko EFRSB (bankruptcy messages) section — paginated and source-linked.

ASSUMED SCHEMA (pending a real sample; kept in this one module so reconciling with the
live API is a local change). Request: ``POST /v2/bankruptcy-messages`` with
``{"key", "inn", "page"}``. Response envelope — only the pagination fields are confirmed:

    {"meta": {"status": "ok"},
     "data": {"ЗапВсего": int, "СтрВсего": int, "СтрТекущ": int,
              "Записи": [{"Дата": "YYYY-MM-DD", "Тип": str, "Номер": str}, ...]}}

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
from claims_assistant.infrastructure.checko.errors import ApiRejected, InvalidResponse

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
    records = data.get("Записи")
    total_pages = data.get("СтрВсего")
    current_page = data.get("СтрТекущ")
    if (
        not isinstance(records, list)
        or type(total_pages) is not int
        or type(current_page) is not int
        or total_pages < 1
        or current_page < 1
    ):
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
        kind = record.get("Тип") if isinstance(record, dict) else None
        label = kind.strip() if isinstance(kind, str) and kind.strip() else "Сообщение ЕФРСБ"
        number = record.get("Номер") if isinstance(record, dict) else None
        record_id = number.strip() if isinstance(number, str) and number.strip() else None
        item = Evidence(f"efrsb-{index}", SOURCE, record_id or f"{inn}:{index}")
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
