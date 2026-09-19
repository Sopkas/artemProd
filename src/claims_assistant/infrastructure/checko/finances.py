"""S3-04: Checko finances section — two comparable years of revenue and net profit.

ASSUMED SCHEMA (pending a real sample; kept here so reconciling is a local change).
Request: ``POST /v2/finances`` with ``{"key", "inn"}``. Response:

    {"meta": {"status": "ok"},
     "data": {"2023": {"2110": <revenue>, "2400": <net_profit>}, "2024": {...}}}

Years are OKUD annual forms keyed by report-line code: 2110 = выручка, 2400 = чистая
прибыль. Amounts are taken as rubles and tagged with the unit; the adapter only projects
the two latest *consecutive* years as facts. Ratios (growth, revenue drop) belong to the
scoring rules (S3-05), not here: this module never divides or infers a trend.
"""

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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
    Period,
    Section,
)
from claims_assistant.domain.sheet_rules import number_text
from claims_assistant.infrastructure.checko.errors import ApiRejected, InvalidResponse

__all__ = ["read_finances", "project_finances", "AiohttpFinancesTransport", "FinancesTransport"]

SOURCE = "checko-finances-v2"
FINANCES_URL = "https://api.checko.ru/v2/finances"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
UNIT = "RUB"
_LINES = (("2110", FactKind.REVENUE, "revenue"), ("2400", FactKind.NET_PROFIT, "net-profit"))


class FinancesTransport(Protocol):
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]: ...


class AiohttpFinancesTransport:
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                FINANCES_URL, json={"key": key, "inn": inn}, allow_redirects=False
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


def read_finances(payload: object) -> dict[int, dict]:
    """Validate the envelope; return {year: line-code map} for four-digit years only."""
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
    return {
        int(key): value
        for key, value in data.items()
        if isinstance(key, str) and key.isdigit() and len(key) == 4 and isinstance(value, dict)
    }


def _amount(value: object) -> Decimal | None:
    """Exact monetary value; floats go through str so binary noise is not introduced."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        text: str | None = str(value)
    elif isinstance(value, str):
        text = number_text(value) or None
    else:
        return None
    if text is None:
        return None
    try:
        dec = Decimal(text)
    except InvalidOperation:
        return None
    return dec if dec.is_finite() else None


def _year_facts(inn: str, year: int, line: dict) -> tuple[list[Fact], Evidence | None]:
    item = Evidence(f"finances-{year}", SOURCE, str(year))
    period = Period(date(year, 1, 1), date(year, 12, 31))
    facts = []
    for code, kind, name in _LINES:
        amount = _amount(line.get(code))
        if amount is None:
            continue
        facts.append(
            Fact(f"{name}-{year}", inn, kind, amount, (item.id,), period=period, unit=UNIT)
        )
    return facts, (item if facts else None)


def project_finances(inn: str, years: dict[int, dict], fetched_at: datetime) -> ExternalSnapshot:
    """Project the two latest consecutive years; otherwise report a partial reason."""
    missing = []
    ordered = sorted(years)
    pair = next(((y - 1, y) for y in reversed(ordered) if y - 1 in years), None)
    if not ordered:
        chosen: list[int] = []
    elif pair is None:
        missing.append("Нет двух сопоставимых последовательных лет; динамика не рассчитывается.")
        chosen = ordered[-1:]
    else:
        chosen = list(pair)

    facts: list[Fact] = []
    evidence: list[Evidence] = []
    for year in chosen:
        year_facts, item = _year_facts(inn, year, years[year])
        facts.extend(year_facts)
        if item is not None:
            evidence.append(item)

    if not facts:
        missing.append("Финансовая отчётность недоступна или пуста.")

    covered = Period(date(pair[0], 1, 1), date(pair[1], 12, 31)) if pair and facts else None
    coverage = Coverage.COMPLETE if not missing else Coverage.PARTIAL
    return ExternalSnapshot(
        inn=inn,
        section=Section.FINANCES,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=FetchStatus.OK,
        coverage=coverage,
        facts=tuple(facts),
        evidence=tuple(evidence),
        missing=tuple(missing),
        covered_period=covered,
    )
