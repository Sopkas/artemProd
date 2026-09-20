"""S3-06: contract examples for all three Checko sections.

Each scenario from the task — empty data, an unknown company, an unknown event, an
exhausted quota, a timeout and a changed structure — runs through the provider on the
examples in ``tests/fixtures/checko``. The invariant under test: an unexpected answer
never turns into "complete, no risk".

The ``*_live_shape`` and ``*_not_found`` examples carry the field names and the envelope
of the real API, checked against it on 20.09.2026 (free tariff); their values are
synthetic, and no real response is stored in the repository.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import Coverage, FactKind, FetchStatus, Section
from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider

INN = "1234567894"
NOW = datetime(2026, 9, 18, tzinfo=UTC)
KEY = "synthetic-private-key"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "checko"
QUOTA_MESSAGE = "Исчерпан лимит запросов по тарифу"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Answer:
    """Replies the same (status, payload) to every call, whatever the section's arguments."""

    def __init__(self, payload=None, status=200, error=None):
        self.payload, self.status, self.error = payload, status, error
        self.calls = 0

    async def request(self, *args):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.status, self.payload


async def fetch(section: Section, transport: Answer):
    transports = {
        Section.COMPANY: {"transport": transport},
        Section.BANKRUPTCY: {"bankruptcy_transport": transport},
        Section.FINANCES: {"finances_transport": transport},
    }[section]
    company = transports.pop("transport", Answer(fixture("company_ok")))
    provider = CheckoCompanyDataProvider(KEY, company, lambda: NOW, **transports)
    (snapshot,) = await provider.fetch(CompanyDataRequest(INN, (section,)))
    return snapshot


def is_no_risk(snapshot) -> bool:
    return snapshot.status is FetchStatus.OK and snapshot.coverage is Coverage.COMPLETE


# --- the live shapes: what the API actually answers ---


@pytest.mark.parametrize(
    "section,name",
    [
        (Section.COMPANY, "company_not_found"),
        (Section.BANKRUPTCY, "efrsb_not_found"),
        (Section.FINANCES, "finances_not_found"),
    ],
)
async def test_an_unknown_company_is_not_found_not_a_broken_answer(section, name):
    """The API answers HTTP 200 and «ok» with an empty data and a message."""
    snapshot = await fetch(section, Answer(fixture(name)))
    assert snapshot.status is FetchStatus.NOT_FOUND
    assert snapshot.error.code == "not_found"
    assert snapshot.coverage is Coverage.UNAVAILABLE and not is_no_risk(snapshot)


async def test_efrsb_records_are_read_by_their_real_field_names():
    snapshot = await fetch(Section.BANKRUPTCY, Answer(fixture("efrsb_live_shape")))
    assert [fact.value for fact in snapshot.facts] == [
        "Сведения о судебном акте, дело А00-0000/2025",  # ТипНаим plus the case number
        "Сведения о получении требования кредитора",  # no case number in the record
    ]
    first, second = snapshot.evidence
    assert first.record_id == "00000000000000000000000000000001"  # GUID, not a surrogate
    assert first.url == "https://fedresurs.ru/bankruptmessage/" + first.record_id
    assert second.url is None  # only the source's own https link is kept
    # The machine code is not shown and no procedure is inferred from it.
    assert all("ArbitralDecree" not in str(fact.value) for fact in snapshot.facts)
    assert any("требует проверки" in reason for reason in snapshot.missing)


async def test_finances_are_read_beside_the_extra_blocks_of_the_live_answer():
    snapshot = await fetch(Section.FINANCES, Answer(fixture("finances_live_shape")))
    assert snapshot.coverage is Coverage.COMPLETE
    by_kind = {(fact.kind, fact.period.end.year): fact.value for fact in snapshot.facts}
    assert by_kind[(FactKind.REVENUE, 2024)] == Decimal("544581317000")
    assert by_kind[(FactKind.NET_PROFIT, 2024)] == Decimal("-11681168000")


# --- baseline: the example answers themselves are accepted ---


@pytest.mark.parametrize(
    "section,name",
    [
        (Section.COMPANY, "company_ok"),
        (Section.BANKRUPTCY, "efrsb_empty_one_page"),
        (Section.FINANCES, "finances_ok"),
    ],
)
async def test_reference_examples_are_accepted(section, name):
    snapshot = await fetch(section, Answer(fixture(name)))
    assert snapshot.status is FetchStatus.OK
    assert all(fact.evidence_ids for fact in snapshot.facts)


# --- empty data ---


async def test_empty_company_without_the_message_is_invalid_not_absent():
    snapshot = await fetch(Section.COMPANY, Answer(fixture("company_empty")))
    assert snapshot.status is FetchStatus.INVALID_RESPONSE
    assert snapshot.coverage is Coverage.UNAVAILABLE


@pytest.mark.parametrize("name", ["efrsb_empty_one_page", "efrsb_empty_zero_pages"])
async def test_empty_efrsb_is_a_complete_sample_without_messages(name):
    snapshot = await fetch(Section.BANKRUPTCY, Answer(fixture(name)))
    assert snapshot.status is FetchStatus.OK
    assert snapshot.coverage is Coverage.COMPLETE
    assert snapshot.facts == ()


async def test_zero_pages_with_records_is_inconsistent():
    payload = fixture("efrsb_empty_zero_pages")
    payload["data"]["Записи"] = [{"Дата": "2026-08-10", "Номер": "X"}]
    snapshot = await fetch(Section.BANKRUPTCY, Answer(payload))
    assert snapshot.status is FetchStatus.INVALID_RESPONSE


async def test_empty_finances_are_partial_not_zero():
    snapshot = await fetch(Section.FINANCES, Answer(fixture("finances_empty")))
    assert snapshot.coverage is Coverage.PARTIAL
    assert snapshot.facts == ()
    assert any("недоступна" in reason for reason in snapshot.missing)


# --- unknown event ---


async def test_unknown_event_type_is_kept_and_flagged_for_review():
    snapshot = await fetch(Section.BANKRUPTCY, Answer(fixture("efrsb_unknown_event")))
    (fact,) = snapshot.facts
    assert fact.kind is FactKind.BANKRUPTCY_EVENT
    assert fact.value == "Сообщение неизвестного типа"
    assert snapshot.evidence[0].record_id == "SYN-1"
    assert snapshot.coverage is Coverage.PARTIAL
    assert any("требует проверки" in reason for reason in snapshot.missing)


# --- exhausted quota ---


@pytest.mark.parametrize("section", list(Section))
async def test_quota_error_inside_http_200_is_safe_and_not_retried(section, caplog):
    transport = Answer(fixture("api_error_quota"))
    snapshot = await fetch(section, transport)
    assert snapshot.status is FetchStatus.UNAVAILABLE
    assert snapshot.error.code == "api_error"
    assert transport.calls == 1
    assert QUOTA_MESSAGE not in repr(snapshot) + caplog.text


@pytest.mark.parametrize("section", list(Section))
async def test_http_429_is_rate_limited_for_every_section(section):
    transport = Answer(status=429)
    snapshot = await fetch(section, transport)
    assert snapshot.status is FetchStatus.RATE_LIMITED
    assert snapshot.error.code == "rate_limited"
    assert transport.calls == 1


# --- timeout ---


@pytest.mark.parametrize("section", list(Section))
async def test_timeout_is_a_safe_section_result(section, caplog):
    snapshot = await fetch(section, Answer(error=TimeoutError(KEY)))
    assert snapshot.error.code == "timeout"
    assert snapshot.coverage is Coverage.UNAVAILABLE
    assert KEY not in repr(snapshot) + caplog.text


# --- changed structure ---


@pytest.mark.parametrize(
    "section,name",
    [
        (Section.COMPANY, "company_structure_changed"),
        (Section.BANKRUPTCY, "efrsb_structure_changed"),
        (Section.BANKRUPTCY, "efrsb_record_fields_changed"),
        (Section.FINANCES, "finances_structure_changed"),
    ],
)
async def test_changed_structure_never_reads_as_no_risk(section, name):
    snapshot = await fetch(section, Answer(fixture(name)))
    assert not is_no_risk(snapshot)
    # Either the answer is rejected, or it is kept with an explicit gap.
    assert snapshot.status is FetchStatus.INVALID_RESPONSE or snapshot.missing


async def test_renamed_record_fields_are_reported_not_silently_dropped():
    snapshot = await fetch(Section.BANKRUPTCY, Answer(fixture("efrsb_record_fields_changed")))
    assert snapshot.facts == ()
    assert any("без распознанной даты" in reason for reason in snapshot.missing)
