"""S8-01: open enforcement proceedings from Checko (the bailiffs' database, ФССП).

The customer saw «незавершённые исполнительные производства: 6» on Checko for his debtor
(15.10.2026) and named debts among the three things to watch daily (16.10.2026). The
fixture follows the shape the source documents; its values are synthetic, and the shape is
still to be compared with a live answer.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from claims_assistant.application.check_company import CompanyCheck
from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.application.internal_context import card_assessment
from claims_assistant.domain.external import Coverage, DataMode, FactKind, FetchStatus, Section
from claims_assistant.domain.scoring import assess
from claims_assistant.infrastructure.checko import courts, enforcements
from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram.card import NO_BURDEN, format_brief, format_card

INN = "1234567894"
NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)
KEY = "synthetic-private-key"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "checko"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Answer:
    def __init__(self, payload=None, status=200):
        self.payload, self.status = payload, status
        self.calls = []

    async def request(self, *args):
        self.calls.append(args)
        return self.status, self.payload


async def fetch(payload):
    transport = Answer(payload)
    provider = CheckoCompanyDataProvider(
        KEY, Answer(), lambda: NOW, enforcements_transport=transport
    )
    (snapshot,) = await provider.fetch(CompanyDataRequest(INN, (Section.ENFORCEMENTS,)))
    return snapshot, transport


def values(snapshot, kind):
    return [fact.value for fact in snapshot.facts if fact.kind is kind]


async def test_open_proceedings_their_count_and_what_is_left_to_collect():
    snapshot, transport = await fetch(fixture("enforcements_doc_shape"))
    assert transport.calls == [(KEY, INN, 20)]
    assert snapshot.status is FetchStatus.OK
    # Four records: two open with a remainder, one paid off (left out), one whose
    # remainder the source did not name (counted, outside the sum, and said).
    assert values(snapshot, FactKind.ENFORCEMENTS_OPEN) == [3]
    assert values(snapshot, FactKind.ENFORCEMENT_DEBT_LEFT) == [Decimal("2525000.5")]
    lines = values(snapshot, FactKind.ENFORCEMENT)
    assert lines[0] == (
        # No «от 01.09.2026» here: the date is the fact's own and goes beside it once.
        "№ 3/26/00001-ИП · Налог (сбор), включая пени · остаток 125 000 ₽ · "
        "ОСП по Синтетическому району"
    )
    assert "остаток не указан" in lines[-1]
    assert snapshot.coverage is Coverage.PARTIAL
    assert snapshot.missing == (
        "У 1 производств источник не назвал остаток; в сумму они не вошли.",
    )
    dates = [f.observed_on for f in snapshot.facts if f.kind is FactKind.ENFORCEMENT]
    assert dates == [date(2026, 9, 1), date(2026, 6, 15), date(2026, 10, 1)]


@pytest.mark.parametrize("pages", [0, 1])
async def test_none_is_a_complete_zero_in_either_empty_form(pages):
    payload = {"data": {"СтрВсего": pages, "СтрТекущ": 1, "Записи": []}, "meta": {"status": "ok"}}
    snapshot, _ = await fetch(payload)
    assert snapshot.status is FetchStatus.OK and snapshot.coverage is Coverage.COMPLETE
    assert values(snapshot, FactKind.ENFORCEMENTS_OPEN) == [0]


async def test_more_than_a_page_is_said():
    payload = fixture("enforcements_doc_shape")
    payload["data"]["Записи"] = payload["data"]["Записи"][:2]
    payload["data"]["СтрВсего"] = 3
    snapshot, _ = await fetch(payload)
    assert snapshot.coverage is Coverage.PARTIAL
    assert snapshot.missing[0].startswith("Показаны 2 новейших исполнительных производств")
    assert "всего может быть больше" in snapshot.missing[0]
    assert "Остаток долга — только по показанным производствам." in snapshot.missing
    # Without the source's totals the count of all is unknown, and it says so.
    (total,) = [f for f in snapshot.facts if f.kind is FactKind.ENFORCEMENTS_TOTAL]
    assert total.value is None and total.missing_reason


def live_totals(payload, total, left):
    """The totals the live answer carries (review A on #92), beside the records."""
    payload["data"].update({"ЗапВсего": total, "ОбщКолич": total, "ОстЗадолж": left})
    return payload


async def test_with_every_record_the_source_totals_give_the_debt_and_the_count_is_exact():
    payload = live_totals(fixture("enforcements_doc_shape"), 4, 2525000.5)
    snapshot, _ = await fetch(payload)
    assert values(snapshot, FactKind.ENFORCEMENTS_OPEN) == [3]
    assert values(snapshot, FactKind.ENFORCEMENT_DEBT_LEFT) == [Decimal("2525000.5")]
    assert values(snapshot, FactKind.ENFORCEMENTS_TOTAL) == []  # nothing was cut
    # The source counted the remainder the record did not name: nothing to say about it.
    assert snapshot.coverage is Coverage.COMPLETE


async def test_a_cut_list_counts_the_debt_over_all_and_the_open_ones_as_a_floor():
    """A large company, live 16.10.2026: a hundred newest, 73 with a remainder, more in the
    source. The debt is the source's own total; the count is «at least»."""
    payload = live_totals(fixture("enforcements_doc_shape"), 250, 30_000_000.0)
    payload["data"]["СтрВсего"] = 3
    snapshot, _ = await fetch(payload)
    assert values(snapshot, FactKind.ENFORCEMENTS_OPEN) == [3]
    assert values(snapshot, FactKind.ENFORCEMENT_DEBT_LEFT) == [Decimal("30000000.0")]
    assert values(snapshot, FactKind.ENFORCEMENTS_TOTAL) == [250]
    assert snapshot.coverage is Coverage.PARTIAL
    assert snapshot.missing == (
        "Показаны 4 новейших исполнительных производств из 250 (вместе с оконченными); "
        "незавершённых среди них — 3, всего может быть больше.",
    )
    check = CompanyCheck(INN, (snapshot,))
    brief = format_brief(check, card_assessment(check, None))
    assert (
        "ФССП: незавершённых исполнительных производств — не меньше 3, "
        "остаток долга 30 000 000,00 ₽." in brief
    )


async def test_none_open_on_a_cut_page_is_not_none_at_all():
    """Review A on #107: the newest hundred all finished, older ones may be open — and the
    debt left over all says so. «нет» would contradict it, and so would the one line for
    the courts and the bailiffs together."""
    payload = live_totals(fixture("enforcements_doc_shape"), 250, 30_000_000.0)
    for record in payload["data"]["Записи"]:
        record["ОстЗадолж"] = 0.0  # paid off: finished
    payload["data"]["СтрВсего"] = 3
    snapshot, _ = await fetch(payload)
    assert values(snapshot, FactKind.ENFORCEMENTS_OPEN) == [0]
    empty = courts.project_courts(INN, [], 0, None, snapshot.fetched_at)
    lines = format_brief(
        CompanyCheck(INN, (empty, snapshot)), assess(INN, (empty, snapshot))
    ).splitlines()
    assert NO_BURDEN not in lines
    assert (
        "ФССП: среди новейших производств незавершённых нет, список неполный (всего 250), "
        "остаток долга по всем — 30 000 000,00 ₽." in lines
    )


@pytest.mark.parametrize(
    ("field", "value"), [("ЗапВсего", "сто"), ("ЗапВсего", -1), ("ОстЗадолж", -5)]
)
async def test_broken_totals_are_an_invalid_answer_not_a_guess(field, value):
    payload = fixture("enforcements_doc_shape")
    payload["data"][field] = value
    snapshot, _ = await fetch(payload)
    assert snapshot.status is FetchStatus.INVALID_RESPONSE


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["data"].pop("Записи"),
        lambda p: p["data"].__setitem__("СтрВсего", "один"),
        lambda p: p["data"].__setitem__("СтрВсего", 0),  # records but no pages
    ],
)
async def test_a_changed_structure_is_never_read_as_no_proceedings(change):
    payload = fixture("enforcements_doc_shape")
    change(payload)
    snapshot, _ = await fetch(payload)
    assert snapshot.status is FetchStatus.INVALID_RESPONSE


async def test_the_request_asks_for_the_newest_hundred(monkeypatch):
    sent = []

    class Response:
        status = 200

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b'{"data": {}, "meta": {"status": "ok"}}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def post(self, url, **kwargs):
            assert KEY not in url
            sent.append((url, kwargs["json"]))
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(enforcements.aiohttp, "ClientSession", Session)
    await enforcements.AiohttpEnforcementsTransport().request(KEY, INN, 2)
    assert sent == [
        (enforcements.ENFORCEMENTS_URL, {"key": KEY, "inn": INN, "sort": "-date", "limit": 100})
    ]


async def test_the_brief_card_shows_the_bailiffs_next_to_the_courts():
    demo = await DemoCompanyDataProvider(DemoScenario.ALARM).fetch(CompanyDataRequest(INN))
    check = CompanyCheck(INN, demo)
    brief = format_brief(check, card_assessment(check, None)).splitlines()
    courts = next(i for i, line in enumerate(brief) if line.startswith("Арбитраж:"))
    assert brief[courts + 1] == (
        "ФССП: незавершённых исполнительных производств — 1, остаток долга 125 000,00 ₽."
    )
    full = format_card(check, None)
    assert "Исполнительные производства (ФССП, незавершённые) — проверено полностью" in full
    assert "Остаток долга на 31.12.2025: 125 000,00 ₽" in full


async def test_the_bailiffs_raise_no_priority_by_themselves():
    """No agreed threshold yet: a figure in the card, not a reason."""
    demo = await DemoCompanyDataProvider(DemoScenario.ALARM).fetch(CompanyDataRequest(INN))
    assert {s.code for s in assess(INN, demo).signals} == {"unresolved_bankruptcy_event"}
    ordinary = await DemoCompanyDataProvider().fetch(CompanyDataRequest(INN))
    brief = format_brief(CompanyCheck(INN, ordinary), assess(INN, ordinary))
    # Nothing in the courts either: one line for both, the brief card stays short.
    assert NO_BURDEN in brief.splitlines()


async def test_one_empty_line_for_both_only_when_both_are_empty():
    """The merge of S8-04 and S8-01 (17.10.2026): the brief card kept under 500 signs."""
    alarm = await DemoCompanyDataProvider(DemoScenario.ALARM).fetch(CompanyDataRequest(INN))
    empty = courts.project_courts(INN, [], 0, None, alarm[0].fetched_at)
    empty = replace(empty, mode=DataMode.DEMO)  # one check never mixes demo and live
    no_cases = tuple(empty if s.section is Section.COURTS else s for s in alarm)
    lines = format_brief(CompanyCheck(INN, no_cases), assess(INN, no_cases)).splitlines()
    assert NO_BURDEN not in lines
    assert "Арбитраж: незавершённых дел, где организация ответчик, нет." in lines
    assert any(
        line.startswith("ФССП: незавершённых исполнительных производств — 1") for line in lines
    )
