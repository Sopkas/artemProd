"""S3-04: Checko EFRSB and finances sections — projection, pagination and safe errors.

Field names for records/finances are the module's documented assumption; these tests use
synthetic payloads of that same minimal shape, never stored real responses.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.company_data import CompanyDataRequest, RequestLimits
from claims_assistant.domain.external import Coverage, DataMode, FactKind, FetchStatus, Section
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider
from claims_assistant.infrastructure.checko.errors import ApiRejected, InvalidResponse

INN = "1234567894"
NOW = datetime(2026, 9, 18, tzinfo=UTC)
KEY = "synthetic-private-key"


def efrsb_page(records, total_pages=1, current=1):
    return {
        "meta": {"status": "ok"},
        "data": {
            "ЗапВсего": len(records),
            "СтрВсего": total_pages,
            "СтрТекущ": current,
            "Записи": records,
        },
    }


def fin_payload(years):
    return {"meta": {"status": "ok"}, "data": {str(y): lines for y, lines in years.items()}}


class PagedBankruptcy:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def request(self, key, inn, page, timeout):
        self.calls.append(page)
        return 200, self.pages[page - 1]


class OneShot:
    def __init__(self, status=200, payload=None, error=None):
        self.status, self.payload, self.error = status, payload, error
        self.calls = []

    async def request(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.status, self.payload


def provider(**transports):
    return CheckoCompanyDataProvider(KEY, OneShot(), lambda: NOW, **transports)


# --- EFRSB projection (pure) ---


def test_read_page_rejects_a_malformed_envelope():
    for bad in [
        None,
        {},
        {"meta": {}},
        {"meta": {"status": "ok"}},
        {"meta": {"status": "ok"}, "data": {"Записи": "x", "СтрВсего": 1, "СтрТекущ": 1}},
    ]:
        with pytest.raises(InvalidResponse):
            bankruptcy.read_page(bad, INN)


def test_read_page_reports_api_error_as_rejection():
    with pytest.raises(ApiRejected):
        bankruptcy.read_page({"meta": {"status": "error"}}, INN)


def test_project_bankruptcy_makes_dated_facts_linked_to_source():
    records = [
        {"Дата": "2026-05-01", "Тип": "Публикация о намерении", "Номер": "A-1"},
        {"Дата": "2026-06-15", "Номер": "B-2"},
    ]
    snapshot = bankruptcy.project_bankruptcy(INN, records, NOW, complete=True, unreadable=0)
    assert snapshot.section == Section.BANKRUPTCY and snapshot.status == FetchStatus.OK
    assert len(snapshot.facts) == 2
    assert {f.kind for f in snapshot.facts} == {FactKind.BANKRUPTCY_EVENT}
    assert snapshot.facts[0].value == "Публикация о намерении"
    assert snapshot.facts[1].value == "Сообщение ЕФРСБ"  # missing type falls back
    assert snapshot.facts[0].observed_on.isoformat() == "2026-05-01"
    # Each fact references evidence that carries the record id and source.
    ids = {e.id for e in snapshot.evidence}
    assert all(set(f.evidence_ids) <= ids for f in snapshot.facts)
    assert snapshot.evidence[0].record_id == "A-1"
    # Messages present, but the register does not say in which role (S3-06).
    assert snapshot.coverage == Coverage.PARTIAL
    assert any("Роль организации" in reason for reason in snapshot.missing)


def test_project_bankruptcy_complete_and_empty_has_no_facts():
    snapshot = bankruptcy.project_bankruptcy(INN, [], NOW, complete=True, unreadable=0)
    assert snapshot.coverage == Coverage.COMPLETE
    assert snapshot.facts == () and snapshot.missing == ()


def test_project_bankruptcy_partial_on_incomplete_or_unreadable():
    partial = bankruptcy.project_bankruptcy(INN, [], NOW, complete=False, unreadable=0)
    assert partial.coverage == Coverage.PARTIAL
    records = [{"Тип": "Без даты"}]  # unparseable date is dropped and reported
    dropped = bankruptcy.project_bankruptcy(INN, records, NOW, complete=True, unreadable=0)
    assert dropped.facts == ()
    assert any("без распознанной даты" in reason for reason in dropped.missing)


# --- EFRSB through the provider (pagination + error mapping) ---


async def test_bankruptcy_paginates_and_completes():
    pages = [
        efrsb_page([{"Дата": "2026-01-01", "Номер": "1"}], total_pages=2, current=1),
        efrsb_page([{"Дата": "2026-02-01", "Номер": "2"}], total_pages=2, current=2),
    ]
    transport = PagedBankruptcy(pages)
    (snapshot,) = await provider(bankruptcy_transport=transport).fetch(
        CompanyDataRequest(INN, (Section.BANKRUPTCY,))
    )
    assert transport.calls == [1, 2]
    assert len(snapshot.facts) == 2
    assert snapshot.mode == DataMode.LIVE


async def test_bankruptcy_marks_partial_when_pages_exceed_budget():
    pages = [
        efrsb_page([{"Дата": "2026-01-01", "Номер": str(i)}], total_pages=5, current=i)
        for i in range(1, 6)
    ]
    transport = PagedBankruptcy(pages)
    limits = RequestLimits(timeout_seconds=5, max_requests=2, max_pages=2)
    (snapshot,) = await provider(bankruptcy_transport=transport).fetch(
        CompanyDataRequest(INN, (Section.BANKRUPTCY,), limits)
    )
    assert transport.calls == [1, 2]  # stopped at the budget, no retries
    assert snapshot.coverage == Coverage.PARTIAL
    assert any("неполная" in reason for reason in snapshot.missing)


@pytest.mark.parametrize(
    "transport,code",
    [
        (OneShot(status=429), "rate_limited"),
        (OneShot(status=403), "access_denied"),
        (OneShot(status=500), "http_error"),
        (OneShot(payload={"meta": {"status": "error"}}), "api_error"),
        (OneShot(payload={"meta": {"status": "ok"}}), "invalid_response"),
        (OneShot(error=TimeoutError(KEY)), "timeout"),
    ],
)
async def test_bankruptcy_failures_are_safe(transport, code):
    (snapshot,) = await provider(bankruptcy_transport=transport).fetch(
        CompanyDataRequest(INN, (Section.BANKRUPTCY,))
    )
    assert snapshot.coverage == Coverage.UNAVAILABLE
    assert snapshot.error.code == code
    assert KEY not in repr(snapshot)


# --- Finances ---


def test_finances_projects_two_consecutive_years():
    years = finances.read_finances(
        fin_payload({2023: {"2110": 1000, "2400": -50}, 2024: {"2110": "2 000,50", "2400": 75.5}})
    )
    snapshot = finances.project_finances(INN, years, NOW)
    assert snapshot.coverage == Coverage.COMPLETE and snapshot.missing == ()
    by = {(f.kind, f.period.end.year): f for f in snapshot.facts}
    assert by[(FactKind.REVENUE, 2024)].value == Decimal("2000.50")
    assert by[(FactKind.REVENUE, 2024)].unit == "RUB"
    assert by[(FactKind.NET_PROFIT, 2023)].value == Decimal("-50")
    assert snapshot.covered_period.start.year == 2023 and snapshot.covered_period.end.year == 2024


def test_finances_partial_without_two_consecutive_years():
    years = finances.read_finances(fin_payload({2020: {"2110": 1}, 2024: {"2110": 2}}))
    snapshot = finances.project_finances(INN, years, NOW)
    assert snapshot.coverage == Coverage.PARTIAL
    assert any("последовательных" in reason for reason in snapshot.missing)
    assert snapshot.covered_period is None


def test_finances_partial_when_empty():
    snapshot = finances.project_finances(INN, finances.read_finances(fin_payload({})), NOW)
    assert snapshot.coverage == Coverage.PARTIAL
    assert snapshot.facts == ()
    assert any("недоступна" in reason for reason in snapshot.missing)


async def test_finances_through_provider_and_api_error(caplog):
    transport = OneShot(
        payload=fin_payload({2023: {"2110": 10, "2400": 1}, 2024: {"2110": 20, "2400": 2}})
    )
    (snapshot,) = await provider(finances_transport=transport).fetch(
        CompanyDataRequest(INN, (Section.FINANCES,))
    )
    assert snapshot.coverage == Coverage.COMPLETE and len(snapshot.facts) == 4
    rejected = OneShot(payload={"meta": {"status": "error"}})
    (bad,) = await provider(finances_transport=rejected).fetch(
        CompanyDataRequest(INN, (Section.FINANCES,))
    )
    assert bad.error.code == "api_error"
    assert KEY not in repr(snapshot) + caplog.text
