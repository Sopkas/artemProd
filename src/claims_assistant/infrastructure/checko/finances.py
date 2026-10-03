"""S3-04: Checko finances section — two comparable years of revenue and net profit.

CONFIRMED against the live API on 20.09.2026 (free tariff; see docs/integrations.md).
Request: ``POST /v2/finances`` with ``{"key", "inn"}``. Response:

    {"meta": {"status": "ok"},
     "data": {"2023": {"2110": <revenue>, "2400": <net_profit>}, "2024": {...}}}

Years are OKUD annual forms keyed by report-line code: 2110 = выручка, 2400 = чистая
прибыль. Amounts are taken as rubles and tagged with the unit; the adapter only projects
the two latest *consecutive* years that carry at least one of these lines — a year key
alone (an empty form, a balance sheet without revenue) is not data. Ratios (growth,
revenue drop) belong to the scoring rules (S3-05), not here: this module never divides or
infers a trend.
"""

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
from claims_assistant.infrastructure.checko.errors import (
    ApiRejected,
    InvalidResponse,
    NotFound,
    is_not_found,
)
from claims_assistant.infrastructure.checko.http import read_response

__all__ = ["read_finances", "project_finances", "AiohttpFinancesTransport", "FinancesTransport"]

SOURCE = "checko-finances-v2"
FINANCES_URL = "https://api.checko.ru/v2/finances"
UNIT = "RUB"
_LINES = (("2110", FactKind.REVENUE, "revenue"), ("2400", FactKind.NET_PROFIT, "net-profit"))
_TITLES = {"revenue": "выручка", "net-profit": "чистая прибыль"}


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
                return await read_response(response)


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
    # Empty reports and an unknown company look alike in ``data``; the message tells them
    # apart, and «no such company» must not read as «no reports».
    if not data and is_not_found(payload["meta"]):
        raise NotFound()
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


def _has_lines(line: dict) -> bool:
    return any(_amount(line.get(code)) is not None for code, _kind, _name in _LINES)


def project_finances(inn: str, years: dict[int, dict], fetched_at: datetime) -> ExternalSnapshot:
    """Project the two latest consecutive years with data; otherwise say what is missing."""
    missing = []
    ordered = sorted(year for year, line in years.items() if _has_lines(line))
    usable = set(ordered)
    pair = next(((y - 1, y) for y in reversed(ordered) if y - 1 in usable), None)
    if not ordered:
        chosen: list[int] = []
    elif pair is None:
        missing.append("Нет двух сопоставимых последовательных лет; динамика не рассчитывается.")
        chosen = ordered[-1:]
    else:
        chosen = list(pair)
        if ordered[-1] != pair[1]:
            missing.append(
                f"Последний год отчётности ({ordered[-1]}) без предыдущего; "
                f"динамика — по {pair[0]}–{pair[1]}."
            )
        for code, _kind, name in _LINES:
            present = [year for year in pair if _amount(years[year].get(code)) is not None]
            if len(present) == 1:
                absent = pair[0] if present[0] == pair[1] else pair[1]
                missing.append(f"Нет строки {code} ({_TITLES[name]}) за {absent}.")
    # A later year that came without either line is a fact, not noise (review A on #71).
    empty = [year for year in sorted(years) if ordered and year > ordered[-1]]
    if empty:
        shown = ", ".join(str(year) for year in empty)
        tail = f"; динамика — по {pair[0]}–{pair[1]}." if pair else "."
        missing.append(f"Отчётность за {shown} без строк 2110 и 2400{tail}")

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
