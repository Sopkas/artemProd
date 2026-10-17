"""S8-06 (part B): what counts as a change between two checks of one organization.

Snapshots are built by the adapters themselves from synthetic records, so a change in a
label or an identifier there breaks these tests rather than the monitoring quietly.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    DataMode,
    Evidence,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.monitoring import (
    COURT_CLAIM_THRESHOLD,
    GAP_NOTE,
    MONITORED,
    SectionState,
    claim_of,
    compare,
)
from claims_assistant.infrastructure.checko import courts, enforcements, fedresurs
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario

INN = "1234567894"
OGRN = "1234567890123"
YESTERDAY = datetime(2026, 10, 15, 9, tzinfo=UTC)
TODAY = datetime(2026, 10, 16, 9, tzinfo=UTC)


def case(number: str, filed: str, claim: int | None = 500_000, kind: str = "Г") -> dict:
    record = {
        "Номер": number,
        "UUID": f"uuid-{number}",
        "СтрКАД": f"https://kad.arbitr.ru/Card/uuid-{number}",
        "Дата": filed,
        "Суд": "Арбитражный суд Синтетической области",
        "Ист": [{"ИНН": "0000001011", "Наим": "ООО «Синтетический кредитор»"}],
        "Ответ": [{"ИНН": INN, "Наим": "ООО «ТЕСТ»"}],
        "Тип": kind,
    }
    if claim is not None:
        record["СуммИск"] = claim
    return record


def court_snapshot(records: list[dict], at: datetime, total: int | None = None):
    total = len(records) if total is None else total
    return courts.project_courts(INN, records, total, Decimal(0), at)


def proceeding(number: str, started: str, left: float = 125_000.0) -> dict:
    return {"ИспПрНомер": number, "ИспПрДата": started, "ПредмИсп": "Налог", "ОстЗадолж": left}


def enforcement_snapshot(records: list[dict], at: datetime):
    return enforcements.project_enforcements(INN, records, 1, at)


def notice(guid: str, published: str, kind: str = fedresurs.INTENT_TYPE) -> dict:
    return {
        "GUID": guid,
        "URL": f"https://fedresurs.ru/{guid}",
        "Дата": published,
        "Тип": kind,
        "Публикатор": "0000000000001",
        "Участники": [OGRN],
    }


def fedresurs_snapshot(records: list[dict], at: datetime):
    return fedresurs.project_intents(INN, records, len(records), OGRN, at)


def company(status: CompanyStatus, at: datetime) -> ExternalSnapshot:
    evidence = Evidence("company", "checko-company-v2", OGRN)
    return ExternalSnapshot(
        inn=INN,
        section=Section.COMPANY,
        source="checko-company-v2",
        mode=DataMode.LIVE,
        fetched_at=at,
        status=FetchStatus.OK,
        coverage=Coverage.COMPLETE,
        facts=(Fact("status", INN, FactKind.COMPANY_STATUS, status, ("company",), at.date()),),
        evidence=(evidence,),
    )


def empty(section: Section, at: datetime) -> ExternalSnapshot:
    return ExternalSnapshot(
        INN, section, "test", DataMode.LIVE, at, FetchStatus.OK, Coverage.COMPLETE
    )


def failed(section: Section, at: datetime) -> ExternalSnapshot:
    return ExternalSnapshot(
        INN,
        section,
        "test",
        DataMode.LIVE,
        at,
        FetchStatus.UNAVAILABLE,
        Coverage.UNAVAILABLE,
        missing=("Источник не ответил.",),
        error=ProviderError("unavailable", "Источник не ответил."),
    )


def full_day(at: datetime, **sections: ExternalSnapshot) -> list[ExternalSnapshot]:
    """Every watched section; the ones not named are empty and successful."""
    base = {
        Section.COMPANY: company(CompanyStatus.ACTIVE, at),
        Section.BANKRUPTCY: empty(Section.BANKRUPTCY, at),
        Section.COURTS: court_snapshot([], at),
        Section.FEDRESURS: fedresurs_snapshot([], at),
        Section.ENFORCEMENTS: enforcement_snapshot([], at),
    }
    base.update({Section(name): snapshot for name, snapshot in sections.items()})
    return list(base.values())


def test_the_same_answer_twice_is_nothing_new():
    old = [case("А00-1/2026", "2026-09-01")]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(old, TODAY)),
    )
    assert result.changes == ()
    assert result.nothing_new
    assert dict(result.states) == dict.fromkeys(MONITORED, SectionState.COMPARED)


def test_a_new_case_as_the_defendant_from_the_threshold_is_sent():
    old = [case("А00-1/2026", "2026-09-01")]
    new = [case("А00-2/2026", "2026-10-15", claim=100_000), *old]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    (change,) = result.to_notify
    assert change.kind is FactKind.COURT_CASE
    assert change.record_id == "uuid-А00-2/2026"
    assert change.url == "https://kad.arbitr.ru/Card/uuid-А00-2/2026"
    assert change.observed_on == date(2026, 10, 15)
    assert change.text.startswith("Новое арбитражное дело, организация — ответчик: А00-2/2026")
    assert "иск 100 000 ₽" in change.text
    assert not result.nothing_new


def test_a_case_below_the_threshold_is_kept_but_not_sent():
    new = [case("А00-2/2026", "2026-10-15", claim=99_999)]
    result = compare(
        INN,
        full_day(YESTERDAY),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    (change,) = result.changes
    assert not change.notify
    assert result.to_notify == ()
    assert result.nothing_new  # nothing to write about


def test_a_case_without_a_named_claim_is_sent_and_says_so():
    new = [case("А00-2/2026", "2026-10-15", claim=None)]
    result = compare(INN, full_day(YESTERDAY), full_day(TODAY, courts=court_snapshot(new, TODAY)))
    (change,) = result.to_notify
    assert change.text.endswith("(сумма иска не указана)")


def test_a_closed_case_is_not_a_change():
    old = [case("А00-2/2026", "2026-10-01"), case("А00-1/2026", "2026-09-01")]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(old[1:], TODAY)),
    )
    assert result.changes == ()


def test_a_record_is_known_by_its_identifier_not_its_place():
    """The newest first: yesterday's first case is second today, and it is not new."""
    old = [case("А00-1/2026", "2026-09-01")]
    new = [case("А00-2/2026", "2026-10-15"), *old]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    assert [c.record_id for c in result.changes] == ["uuid-А00-2/2026"]


def test_an_older_case_entering_a_cut_list_is_not_new():
    """A hundred newest of more: when one leaves, an older one comes into the page."""
    old = [case("А00-3/2026", "2026-09-10"), case("А00-2/2026", "2026-09-05")]
    new = [case("А00-3/2026", "2026-09-10"), case("А00-1/2026", "2026-08-01")]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY, total=150)),
        full_day(TODAY, courts=court_snapshot(new, TODAY, total=150)),
    )
    assert result.changes == ()


def test_an_older_case_in_a_full_list_is_new():
    """Not cut: a case registered late is still a case that was not there."""
    old = [case("А00-3/2026", "2026-09-10")]
    new = [*old, case("А00-1/2026", "2026-08-01")]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    assert [c.record_id for c in result.changes] == ["uuid-А00-1/2026"]


def test_a_bankruptcy_case_is_sent_whatever_its_claim():
    """The customer's debtor of 15.10.2026: the tax authority's application in court."""
    new = [case("А00-9/2026", "2026-10-15", claim=50_000, kind=courts.BANKRUPTCY_KIND)]
    result = compare(INN, full_day(YESTERDAY), full_day(TODAY, courts=court_snapshot(new, TODAY)))
    (change,) = result.to_notify
    assert change.kind is FactKind.BANKRUPTCY_CASE
    assert change.text.startswith("Новое дело о банкротстве организации в арбитражном суде:")


def test_the_same_case_becoming_a_bankruptcy_is_new():
    """A record seen as an ordinary case and now read as the company's bankruptcy."""
    old = [case("А00-9/2026", "2026-10-01")]
    new = [case("А00-9/2026", "2026-10-01", kind=courts.BANKRUPTCY_KIND)]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    assert [c.kind for c in result.to_notify] == [FactKind.BANKRUPTCY_CASE]


@pytest.mark.parametrize(
    ("kind", "fact", "opening"),
    [
        (fedresurs.INTENT_TYPE, FactKind.BANKRUPTCY_INTENT, "Кредитор объявил о намерении"),
        (fedresurs.FILING_TYPE, FactKind.BANKRUPTCY_FILING, "На Федресурсе — заявление"),
    ],
)
def test_a_new_fedresurs_notice_is_sent(kind, fact, opening):
    result = compare(
        INN,
        full_day(YESTERDAY),
        full_day(TODAY, fedresurs=fedresurs_snapshot([notice("g-1", "2026-10-15", kind)], TODAY)),
    )
    (change,) = result.to_notify
    assert change.kind is fact
    assert change.text.startswith(opening)
    assert change.url == "https://fedresurs.ru/g-1"


def test_a_new_proceeding_is_sent_and_a_finished_one_is_not():
    old = [proceeding("1/26/00001-ИП", "2026-09-01")]
    new = [proceeding("2/26/00001-ИП", "2026-10-14"), proceeding("1/26/00001-ИП", "2026-09-01", 0)]
    result = compare(
        INN,
        full_day(YESTERDAY, enforcements=enforcement_snapshot(old, YESTERDAY)),
        full_day(TODAY, enforcements=enforcement_snapshot(new, TODAY)),
    )
    (change,) = result.to_notify
    assert change.kind is FactKind.ENFORCEMENT
    assert change.record_id == "2/26/00001-ИП"
    assert "остаток 125 000 ₽" in change.text


def test_a_liquidation_is_sent():
    result = compare(
        INN,
        full_day(YESTERDAY),
        full_day(TODAY, company=company(CompanyStatus.LIQUIDATING, TODAY)),
    )
    (change,) = result.to_notify
    assert change.kind is FactKind.COMPANY_STATUS
    assert change.text == (
        "Статус организации изменился: был «действует», теперь «в стадии ликвидации»"
    )


def test_the_first_check_is_a_baseline():
    new = [case("А00-1/2026", "2026-09-01", claim=5_000_000)]
    result = compare(INN, [], full_day(TODAY, courts=court_snapshot(new, TODAY)))
    assert result.changes == ()
    assert dict(result.states) == dict.fromkeys(MONITORED, SectionState.BASELINE)
    assert not result.nothing_new  # nothing was compared, so nothing is known unchanged


def test_a_failed_section_is_unchecked_not_unchanged():
    result = compare(
        INN,
        full_day(YESTERDAY),
        full_day(TODAY, courts=failed(Section.COURTS, TODAY)),
    )
    assert result.changes == ()
    assert result.unchecked == (Section.COURTS,)
    assert (Section.COURTS, "Источник не ответил.") in result.notes
    assert not result.nothing_new


def test_a_section_not_asked_today_is_unchecked():
    today = [s for s in full_day(TODAY) if s.section is not Section.ENFORCEMENTS]
    result = compare(INN, full_day(YESTERDAY), today)
    assert result.unchecked == (Section.ENFORCEMENTS,)
    assert not result.nothing_new


def test_after_a_failed_previous_check_the_section_is_a_baseline():
    """The service passes the last successful snapshot; a failed one has nothing in it."""
    new = [case("А00-1/2026", "2026-10-15")]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=failed(Section.COURTS, YESTERDAY)),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    assert result.changes == ()
    assert dict(result.states)[Section.COURTS] is SectionState.BASELINE


def test_a_partial_answer_is_compared_and_its_reason_kept():
    old = [case("А00-1/2026", "2026-09-01")]
    new = [case("А00-2/2026", "2026-10-15"), *old]
    result = compare(
        INN,
        full_day(YESTERDAY, courts=court_snapshot(old, YESTERDAY, total=150)),
        full_day(TODAY, courts=court_snapshot(new, TODAY, total=150)),
    )
    assert [c.record_id for c in result.to_notify] == ["uuid-А00-2/2026"]
    assert any(section is Section.COURTS and "из 150" in note for section, note in result.notes)


def test_finances_are_not_watched():
    assert Section.FINANCES not in MONITORED
    assert set(MONITORED) | {Section.FINANCES} == set(Section)


def test_snapshots_of_another_inn_are_refused():
    with pytest.raises(ValueError, match="INN"):
        compare("7707083893", full_day(YESTERDAY), full_day(TODAY))


@pytest.mark.parametrize(
    ("claim", "expected"),
    [(2_400_000, Decimal("2400000")), (100_000, COURT_CLAIM_THRESHOLD), (950, Decimal(950))],
)
def test_the_claim_is_read_from_the_adapter_label(claim, expected):
    """The contract between courts.py and the threshold: the label is what is parsed."""
    snapshot = court_snapshot([case("А00-1/2026", "2026-09-01", claim=claim)], TODAY)
    (fact,) = [f for f in snapshot.facts if f.kind is FactKind.COURT_CASE]
    assert claim_of(fact) == expected


def test_a_claimant_name_with_digits_is_not_read_as_the_claim():
    record = case("А00-1/2026", "2026-09-01", claim=None)
    record["Ист"] = [{"ИНН": "1", "Наим": "ООО «иск 900 000 ₽»"}]
    snapshot = court_snapshot([record], TODAY)
    (fact,) = [f for f in snapshot.facts if f.kind is FactKind.COURT_CASE]
    assert claim_of(fact) is None


async def test_demo_alarm_against_ordinary_names_its_cases_and_proceeding():
    """The demo source used in the show: the day the ordinary company turns alarming."""
    request = CompanyDataRequest(INN, MONITORED)
    calm = await DemoCompanyDataProvider(DemoScenario.ORDINARY).fetch(request)
    alarm = await DemoCompanyDataProvider(DemoScenario.ALARM).fetch(request)
    result = compare(INN, calm, alarm)
    kinds = sorted(c.kind for c in result.to_notify)
    assert kinds == sorted(
        [FactKind.BANKRUPTCY_EVENT, FactKind.COURT_CASE, FactKind.COURT_CASE, FactKind.ENFORCEMENT]
    )


# --- a gap after a long break (S8-12) ------------------------------------------------

WEEK_AGO = datetime(2026, 10, 9, 9, tzinfo=UTC)


def test_a_cut_list_that_does_not_reach_back_to_the_last_check_is_compared_in_part():
    """A pause or the weekly mode: a week of a large defendant's cases may be more than the
    hundred the source returns. What is in the list is sent; the rest is said unknown."""
    old = [case("А00-1/2026", "2026-10-01")]
    new = [case("А00-3/2026", "2026-10-15"), case("А00-2/2026", "2026-10-12")]
    result = compare(
        INN,
        full_day(WEEK_AGO, courts=court_snapshot(old, WEEK_AGO, total=150)),
        full_day(TODAY, courts=court_snapshot(new, TODAY, total=400)),
    )
    assert [c.record_id for c in result.to_notify] == ["uuid-А00-3/2026", "uuid-А00-2/2026"]
    assert dict(result.states)[Section.COURTS] is SectionState.PARTLY
    assert result.partly == (Section.COURTS,) and result.unchecked == ()
    (gap,) = [note for section, note in result.notes if note.startswith(GAP_NOTE)]
    assert "(09.10.2026)" in gap and "до 12.10.2026" in gap
    assert not result.nothing_new


def test_a_cut_list_reaching_back_to_the_last_check_is_compared_in_full():
    old = [case("А00-1/2026", "2026-10-01")]
    new = [case("А00-2/2026", "2026-10-12"), case("А00-1/2026", "2026-10-01")]
    result = compare(
        INN,
        full_day(WEEK_AGO, courts=court_snapshot(old, WEEK_AGO, total=150)),
        full_day(TODAY, courts=court_snapshot(new, TODAY, total=151)),
    )
    assert dict(result.states)[Section.COURTS] is SectionState.COMPARED


def test_a_full_list_of_only_new_records_has_no_gap():
    """Not cut: every record is there, however new they all are."""
    new = [case("А00-2/2026", "2026-10-12")]
    result = compare(
        INN,
        full_day(WEEK_AGO),
        full_day(TODAY, courts=court_snapshot(new, TODAY)),
    )
    assert dict(result.states)[Section.COURTS] is SectionState.COMPARED
    assert [c.record_id for c in result.to_notify] == ["uuid-А00-2/2026"]


def test_a_cut_bailiffs_list_is_known_by_its_total():
    """ФССП says a cut by the source's total (#107), not only by a note."""
    records = [proceeding("2/26/1-ИП", "2026-10-14"), proceeding("1/26/1-ИП", "2026-10-13")]
    today = enforcements.project_enforcements(INN, records, 1, TODAY, total=250, left=None)
    result = compare(INN, full_day(WEEK_AGO), full_day(TODAY, enforcements=today))
    assert dict(result.states)[Section.ENFORCEMENTS] is SectionState.PARTLY
    assert len(result.to_notify) == 2


def test_a_cut_fedresurs_list_is_known_by_its_note():
    today = fedresurs.project_intents(INN, [notice("g-2", "2026-10-14")], 300, OGRN, TODAY)
    result = compare(INN, full_day(WEEK_AGO), full_day(TODAY, fedresurs=today))
    assert dict(result.states)[Section.FEDRESURS] is SectionState.PARTLY


def test_a_list_partial_for_another_reason_has_no_gap():
    """Every proceeding came, one without a remainder named: partial, but not cut — the
    list does reach back, however new its records are."""
    nameless = {"ИспПрНомер": "2/26/1-ИП", "ИспПрДата": "2026-10-14", "ПредмИсп": "Налог"}
    today = enforcement_snapshot([nameless, proceeding("1/26/1-ИП", "2026-10-13")], TODAY)
    assert today.coverage is Coverage.PARTIAL
    result = compare(INN, full_day(WEEK_AGO), full_day(TODAY, enforcements=today))
    assert dict(result.states)[Section.ENFORCEMENTS] is SectionState.COMPARED
