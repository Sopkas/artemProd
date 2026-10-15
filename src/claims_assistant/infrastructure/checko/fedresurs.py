"""S8-01: creditors' notices on Fedresurs of their intention to file for bankruptcy.

The case the customer showed on 15.10.2026: the card said «сообщений о банкротстве не
найдено», and so did the register (EFRSB) — while a creditor had already announced on
Fedresurs that it was going to court, and then did. A creditor must publish that notice at
least fifteen days before filing, so it is the earliest public sign of a bankruptcy case.

SCHEMA FROM DOCUMENTATION (Checko API v2.4, «/fedresurs», as described in its public method
reference), not yet compared with a live answer — reconcile here when it is:

    POST https://api.checko.ru/v2/fedresurs
    {"key", "inn", "type": "CreditorIntentionGoToCourt", "sort": "-date", "limit": 100}

    {"company": {"ОГРН": "…", "ИНН": "…"},
     "data": {"ЗапВсего": 1, "СтрВсего": 1, "СтрТекущ": 1,
              "Записи": [{"GUID": "…", "URL": "https://fedresurs.ru/…", "Дата": "2026-03-20",
                          "Тип": "CreditorIntentionGoToCourt",
                          "ТипНаим": "Намерение кредитора обратиться в суд с заявлением о
                                      банкротстве",
                          "Публикатор": "<ОГРН>", "Участники": ["<ОГРН или ИНН>", …]}]},
     "meta": {"status": "ok", ...}}

The source answers by INN in either role. A notice the company published itself is its own
intention against someone else — it is skipped, as is any type other than the intention
(in case the filter is not applied by the source).
"""

from datetime import datetime
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
from claims_assistant.infrastructure.checko.http import read_response

__all__ = ["FedresursTransport", "AiohttpFedresursTransport", "read_intents", "project_intents"]

SOURCE = "checko-fedresurs-v2"
FEDRESURS_URL = "https://api.checko.ru/v2/fedresurs"
INTENT_TYPE = "CreditorIntentionGoToCourt"
INTENT_LABEL = "Намерение кредитора обратиться в суд с заявлением о банкротстве"
PAGE_SIZE = 100


class FedresursTransport(Protocol):
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]: ...


class AiohttpFedresursTransport:
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]:
        # One session per request; key stays in the body, redirects and env proxies disabled.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                FEDRESURS_URL,
                json={
                    "key": key,
                    "inn": inn,
                    "type": INTENT_TYPE,
                    "sort": "-date",
                    "limit": PAGE_SIZE,
                },
                allow_redirects=False,
            ) as response:
                return await read_response(response)


def read_intents(payload: object, inn: str) -> tuple[list[dict], int, str | None]:
    """Validate the envelope; return (records, total in the source, the company's ОГРН)."""
    records, _pages, _current = read_page(payload, inn)
    total = payload["data"].get("ЗапВсего")  # read_page has checked data is a dict
    company = payload.get("company")
    ogrn = _text(company.get("ОГРН"), 20) if isinstance(company, dict) else None
    return records, total if type(total) is int and total >= 0 else len(records), ogrn


def project_intents(
    inn: str, records: list[dict], total: int, ogrn: str | None, fetched_at: datetime
) -> ExternalSnapshot:
    """One fact per creditor's notice against the company; its own notices are left out."""
    facts = []
    evidence = []
    unreadable = 0
    for index, record in enumerate(records[:PAGE_SIZE]):
        if not isinstance(record, dict) or record.get("Тип") != INTENT_TYPE:
            continue
        publisher = _text(record.get("Публикатор"), 20)
        if publisher is not None and publisher in (ogrn, inn):
            continue  # the company announced it would sue someone else
        published_on = _iso_date(record.get("Дата"))
        if published_on is None:
            unreadable += 1
            continue
        record_id = _text(record.get("GUID")) or f"{inn}:{index}"
        item = Evidence(f"fedresurs-{index}", SOURCE, record_id, url=_url(record.get("URL")))
        evidence.append(item)
        facts.append(
            Fact(
                f"fedresurs-intent-{index}",
                inn,
                FactKind.BANKRUPTCY_INTENT,
                _text(record.get("ТипНаим")) or INTENT_LABEL,
                (item.id,),
                observed_on=published_on,
            )
        )
    missing = []
    if total > len(records):
        missing.append(f"Показаны последние {len(records)} сообщений Федресурса из {total}.")
    if unreadable:
        missing.append("Часть сообщений Федресурса без распознанной даты не учтена.")
    if not ogrn and facts:
        missing.append("Источник не назвал ОГРН организации; кто публикатор — не сверено.")
    return ExternalSnapshot(
        inn=inn,
        section=Section.FEDRESURS,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=FetchStatus.OK,
        coverage=Coverage.PARTIAL if missing else Coverage.COMPLETE,
        facts=tuple(facts),
        evidence=tuple(evidence),
        missing=tuple(missing),
    )
