"""S8-01: Checko enforcement proceedings (the bailiffs' database, ФССП).

What the customer saw on Checko for his debtor on 15.10.2026 — «незавершённые исполнительные
производства: 6» — and named among the three things to watch daily (16.10.2026): an
enforcement proceeding is a debt a court or the tax authority has already decided, and a
bailiff is collecting it.

SCHEMA FROM DOCUMENTATION (the method reference of the source), not yet compared with a live
answer — reconcile here when it is:

    POST https://api.checko.ru/v2/enforcements
    {"key", "inn", "sort": "-date", "limit": 100}

    {"data": {"СтрВсего": 1, "СтрТекущ": 1,
              "Записи": [{"ДолжНаим": "…", "ДолжАдрес": "…",
                          "ИспПрНомер": "12345/26/38001-ИП", "ИспПрДата": "2026-09-01",
                          "ИспДокНомер": "…", "ИспДокТип": "Акт органа, осуществляющего
                                                            контрольные функции",
                          "ИспДокДата": "2026-08-20",
                          "ПредмИсп": "Налог (сбор), включая пени",
                          "СудПристНаим": "ОСП по …", "СудПристАдрес": "…",
                          "СумДолг": 125000.0, "ОстЗадолж": 125000.0}]},
     "meta": {"status": "ok", ...}}

The documentation names no count of all records and no end date of a proceeding. So the
count is of the records received, a proceeding with nothing left to pay (``ОстЗадолж`` 0)
is taken as finished and left out, and one whose remainder the source did not name is kept
— counted, but outside the sum — rather than guessed paid.
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
from claims_assistant.infrastructure.checko.bankruptcy import _iso_date, _text
from claims_assistant.infrastructure.checko.errors import (
    ApiRejected,
    InvalidResponse,
    NotFound,
    is_not_found,
)
from claims_assistant.infrastructure.checko.finances import _amount
from claims_assistant.infrastructure.checko.http import read_response

__all__ = [
    "EnforcementsTransport",
    "AiohttpEnforcementsTransport",
    "read_enforcements",
    "project_enforcements",
]

SOURCE = "checko-enforcements-v2"
ENFORCEMENTS_URL = "https://api.checko.ru/v2/enforcements"
PAGE_SIZE = 100
UNIT = "RUB"


class EnforcementsTransport(Protocol):
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]: ...


class AiohttpEnforcementsTransport:
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]:
        # One session per request; key stays in the body, redirects and env proxies disabled.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                ENFORCEMENTS_URL,
                json={"key": key, "inn": inn, "sort": "-date", "limit": PAGE_SIZE},
                allow_redirects=False,
            ) as response:
                return await read_response(response)


def read_enforcements(payload: object) -> tuple[list[dict], int]:
    """Validate the envelope; return (records of the first page, pages in the source).

    Like the other list methods, but without a count of all records: an empty answer is
    taken in whichever form it comes — zero pages or one page with no records.
    """
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
    pages = data.get("СтрВсего")
    if not isinstance(records, list) or type(pages) is not int or pages < 0:
        raise InvalidResponse()
    if records and pages < 1:
        raise InvalidResponse()
    return records, pages


def _label(record: dict, number: str, started: str) -> str:
    """The proceeding as a person reads it: number and date, what is collected, how much is
    left, and the bailiffs' office."""
    parts = [f"№ {number} от {started}" if number else f"от {started}"]
    subject = _text(record.get("ПредмИсп"), 200)
    if subject:
        parts.append(subject)
    left = _amount(record.get("ОстЗадолж"))
    if left is not None:
        parts.append(f"остаток {f'{left:,.0f}'.replace(',', ' ')} ₽")
    else:
        parts.append("остаток не указан")
    office = _text(record.get("СудПристНаим"), 200)
    if office:
        parts.append(office)
    return " · ".join(parts)[:500]


def project_enforcements(
    inn: str, records: list[dict], pages: int, fetched_at: datetime
) -> ExternalSnapshot:
    """Open proceedings: how many, how much is left to collect, and each of them."""
    as_of = fetched_at.date()
    summary = Evidence("enforcements", SOURCE, inn)
    evidence = [summary]
    facts: list[Fact] = []
    left_total = Decimal(0)
    open_count = 0
    unreadable = 0
    no_remainder = 0
    for index, record in enumerate(records[:PAGE_SIZE]):
        started_on = _iso_date(record.get("ИспПрДата")) if isinstance(record, dict) else None
        if started_on is None:
            unreadable += 1
            continue
        left = _amount(record.get("ОстЗадолж"))
        if left is not None and left <= 0:
            continue  # nothing left to collect: the proceeding is over
        open_count += 1
        if left is None:
            no_remainder += 1
        else:
            left_total += left
        number = _text(record.get("ИспПрНомер"), 100) or ""
        item = Evidence(f"enforcement-{index}", SOURCE, number or f"{inn}:{index}")
        evidence.append(item)
        facts.append(
            Fact(
                f"enforcement-{index}",
                inn,
                FactKind.ENFORCEMENT,
                _label(record, number, f"{started_on:%d.%m.%Y}"),
                (item.id,),
                observed_on=started_on,
            )
        )
    facts[:0] = [
        Fact(
            "enforcements-open",
            inn,
            FactKind.ENFORCEMENTS_OPEN,
            open_count,
            (summary.id,),
            observed_on=as_of,
        ),
        Fact(
            "enforcements-left",
            inn,
            FactKind.ENFORCEMENT_DEBT_LEFT,
            left_total,
            (summary.id,),
            observed_on=as_of,
            unit=UNIT,
        ),
    ]
    missing = []
    if pages > 1:
        missing.append(
            f"Показаны {len(records)} новейших исполнительных производств; в источнике их больше."
        )
    if unreadable:
        missing.append("Часть производств без распознанной даты не учтена.")
    if no_remainder:
        missing.append(
            f"У {no_remainder} производств источник не назвал остаток; в сумму они не вошли."
        )
    return ExternalSnapshot(
        inn=inn,
        section=Section.ENFORCEMENTS,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=FetchStatus.OK,
        coverage=Coverage.PARTIAL if missing else Coverage.COMPLETE,
        facts=tuple(facts),
        evidence=tuple(evidence),
        missing=tuple(missing),
    )
