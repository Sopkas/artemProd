"""S8-01: creditors' notices on Fedresurs of their intention to file for bankruptcy.

The case the customer showed on 15.10.2026: the card said «сообщений о банкротстве не
найдено», and so did the register (EFRSB) — while a creditor had already announced on
Fedresurs that it was going to court, and then did. A creditor must publish that notice at
least fifteen days before filing, so it is the earliest public sign of a bankruptcy case.

CHECKED LIVE on 15.10.2026 (developer A, the customer's debtor). The application there was
the tax authority's, and it does not announce an intention: it publishes the filing itself,
``DebtorBankruptcyCourtNotification``, with the debtor among ``Участники``. So the request
asks for every type — one page of the newest hundred — and two types are kept here:

    POST https://api.checko.ru/v2/fedresurs
    {"key", "inn", "sort": "-date", "limit": 100}

    {"company": {"ОГРН": "…", "ИНН": "…"},
     "data": {"ЗапВсего": 1, "СтрВсего": 1, "СтрТекущ": 1,
              "Записи": [{"GUID": "…", "URL": "https://fedresurs.ru/…", "Дата": "2026-03-20",
                          "Тип": "CreditorIntentionGoToCourt",
                          "ТипНаим": "Намерение кредитора обратиться в суд с заявлением о
                                      банкротстве",
                          "Публикатор": "<ОГРН>", "Участники": ["<ОГРН или ИНН>", …]}]},
     "meta": {"status": "ok", ...}}

The source answers by INN in either role. A notice counts when the company is among
``Участники`` and is not its publisher: a notice it published itself is its own claim against
someone else. ``Публикатор`` may be null. Other types — leasing contracts, the bulk of a
lessee's messages — are left out here.
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
FILING_TYPE = "DebtorBankruptcyCourtNotification"
FILING_LABEL = "Заявление уполномоченного органа о признании должника банкротом направлено в суд"
# The types kept, and the fact each becomes.
KINDS = {INTENT_TYPE: FactKind.BANKRUPTCY_INTENT, FILING_TYPE: FactKind.BANKRUPTCY_FILING}
LABELS = {INTENT_TYPE: INTENT_LABEL, FILING_TYPE: FILING_LABEL}
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
                json={"key": key, "inn": inn, "sort": "-date", "limit": PAGE_SIZE},
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
    """One fact per notice about the company's bankruptcy; its own notices are left out."""
    facts = []
    evidence = []
    unreadable = 0
    ours = {code for code in (ogrn, inn) if code}
    for index, record in enumerate(records[:PAGE_SIZE]):
        if not isinstance(record, dict) or record.get("Тип") not in KINDS:
            continue
        publisher = _text(record.get("Публикатор"), 20)
        if publisher is not None and publisher in ours:
            continue  # the company announced it would sue someone else
        participants = record.get("Участники")
        # Participants are named by ОГРН: without the company's own it cannot be matched,
        # and dropping every notice would read as «nothing found» — so it is not checked.
        if (
            ogrn
            and isinstance(participants, list)
            and not ours.intersection(str(code) for code in participants)
        ):
            continue  # a notice about someone else that names the company otherwise
        published_on = _iso_date(record.get("Дата"))
        if published_on is None:
            unreadable += 1
            continue
        record_id = _text(record.get("GUID")) or f"{inn}:{index}"
        item = Evidence(f"fedresurs-{index}", SOURCE, record_id, url=_url(record.get("URL")))
        evidence.append(item)
        kind = KINDS[record["Тип"]]
        facts.append(
            Fact(
                f"fedresurs-{kind.value}-{index}",
                inn,
                kind,
                _text(record.get("ТипНаим")) or LABELS[record["Тип"]],
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
        missing.append(
            "Источник не назвал ОГРН организации; кто публикатор и участник — не сверено."
        )
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
