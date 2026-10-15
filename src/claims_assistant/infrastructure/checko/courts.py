"""S8-01: Checko arbitration cases where the company is the defendant and the case is open.

SCHEMA FROM DOCUMENTATION (Checko API v2.3 as described in its public method reference),
not yet compared with a live answer — reconcile here when it is:

    POST https://api.checko.ru/v2/legal-cases
    {"key", "inn", "role": "defendant", "active": "true", "sort": "-date", "limit": 100}

    {"data": {"ЗапВсего": 14, "СтрВсего": 1, "СтрТекущ": 1, "ОбщСуммИск": 12300000,
              "Записи": [{"Номер": "А19-1234/2026", "UUID": "…", "СтрКАД": "https://kad…",
                          "Дата": "2026-09-24", "Суд": "Арбитражный суд …",
                          "Ист": [{"ИНН", "Наим", "Адрес"}], "Ответ": [...],
                          "СуммИск": 500000}]},
     "meta": {"status": "ok", ...}}

Why only open cases as the defendant: that is the claim load — what the company may yet
have to pay — and what the customer asked to watch (15.10.2026). Cases where it sues
someone else say nothing against it. The source does not say which case is a bankruptcy
case; the creditors' notices of intention (``fedresurs``) and the register (EFRSB) do.

One page of the newest hundred: the count and the sum of claims come from the source's own
totals, so a busy defendant is not cut down to a hundred cases; only the list is.
"""

from datetime import datetime
from decimal import Decimal
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
from claims_assistant.infrastructure.checko.bankruptcy import _iso_date, _text, _url, read_page
from claims_assistant.infrastructure.checko.errors import InvalidResponse
from claims_assistant.infrastructure.checko.finances import _amount
from claims_assistant.infrastructure.checko.http import read_response

__all__ = ["CourtsTransport", "AiohttpCourtsTransport", "read_courts", "project_courts"]

SOURCE = "checko-legal-cases-v2"
COURTS_URL = "https://api.checko.ru/v2/legal-cases"
PAGE_SIZE = 100
UNIT = "RUB"


class CourtsTransport(Protocol):
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]: ...


class AiohttpCourtsTransport:
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]:
        # One session per request; key stays in the body, redirects and env proxies disabled.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                COURTS_URL,
                json={
                    "key": key,
                    "inn": inn,
                    "role": "defendant",
                    "active": "true",
                    "sort": "-date",
                    "limit": PAGE_SIZE,
                },
                allow_redirects=False,
            ) as response:
                return await read_response(response)


def read_courts(payload: object, inn: str) -> tuple[list[dict], int, Decimal | None]:
    """Validate the envelope; return (records, total cases, total of claims)."""
    records, _pages, _current = read_page(payload, inn)
    data = payload["data"]  # read_page has checked it is a dict
    total = data.get("ЗапВсего")
    if type(total) is not int or total < 0:
        raise InvalidResponse()
    claims = _amount(data.get("ОбщСуммИск"))
    if claims is not None and claims < 0:
        raise InvalidResponse()
    return records, total, claims


def _label(record: dict) -> str:
    """The case as a person reads it: number, court and the sum claimed."""
    parts = [_text(record.get("Номер"), 100) or "номер не указан"]
    court = _text(record.get("Суд"), 200)
    if court:
        parts.append(court)
    claim = _amount(record.get("СуммИск"))
    if claim is not None and claim > 0:
        whole = f"{claim:,.0f}".replace(",", " ")
        parts.append(f"иск {whole} ₽")
    plaintiffs = record.get("Ист")
    if isinstance(plaintiffs, list):
        names = [_text(p.get("Наим"), 150) for p in plaintiffs if isinstance(p, dict)]
        names = [name for name in names if name]
        if names:
            parts.append("истец: " + (names[0] if len(names) == 1 else f"{names[0]} и др."))
    return " · ".join(parts)[:500]


def project_courts(
    inn: str, records: list[dict], total: int, claims: Decimal | None, fetched_at: datetime
) -> ExternalSnapshot:
    """The claim load: how many open cases, their sum, and the newest of them."""
    as_of = fetched_at.date()
    summary = Evidence("legal-cases", SOURCE, inn)
    evidence = [summary]
    facts = [
        Fact(
            "court-cases-active",
            inn,
            FactKind.COURT_CASES_ACTIVE,
            total,
            (summary.id,),
            observed_on=as_of,
        )
    ]
    if claims is not None:
        facts.append(
            Fact(
                "court-claims-total",
                inn,
                FactKind.COURT_CLAIMS_TOTAL,
                claims,
                (summary.id,),
                observed_on=as_of,
                unit=UNIT,
            )
        )
    unreadable = 0
    for index, record in enumerate(records[:PAGE_SIZE]):
        filed_on = _iso_date(record.get("Дата")) if isinstance(record, dict) else None
        if filed_on is None:
            unreadable += 1
            continue
        record_id = _text(record.get("UUID")) or _text(record.get("Номер")) or f"{inn}:{index}"
        item = Evidence(f"court-{index}", SOURCE, record_id, url=_url(record.get("СтрКАД")))
        evidence.append(item)
        facts.append(
            Fact(
                f"court-case-{index}",
                inn,
                FactKind.COURT_CASE,
                _label(record),
                (item.id,),
                observed_on=filed_on,
            )
        )
    missing = []
    if total > len(records):
        missing.append(
            f"Показаны последние {len(records)} дел из {total}; число дел и сумма исков — по всем."
        )
    if unreadable:
        missing.append("Часть дел без распознанной даты не показана.")
    if claims is None and total:
        missing.append("Источник не назвал общую сумму исков.")
    return ExternalSnapshot(
        inn=inn,
        section=Section.COURTS,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=FetchStatus.OK,
        coverage=Coverage.PARTIAL if missing else Coverage.COMPLETE,
        facts=tuple(facts),
        evidence=tuple(evidence),
        missing=tuple(missing),
    )
