"""S8-01: arbitration cases as the defendant and creditors' notices of a bankruptcy filing.

The customer's debtor of 15.10.2026: the card said «сообщений о банкротстве не найдено» —
the register (EFRSB) was indeed empty — while a creditor had announced on Fedresurs that it
was going to court, and fourteen cases stood against the company. The fixtures follow the
response shape the source documents (``tests/fixtures/checko/courts_*``, ``fedresurs_*``);
their values are synthetic. The shape is still to be compared with a live answer.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import Coverage, FactKind, FetchStatus, Section
from claims_assistant.domain.scoring import (
    INTENT_WINDOW_DAYS,
    Assessment,
    Priority,
    Signal,
    assess,
)
from claims_assistant.infrastructure.checko import company_data as checko
from claims_assistant.infrastructure.checko import courts, fedresurs
from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario

INN = "1234567894"
OGRN = "1234567890123"
NOW = datetime(2026, 10, 15, 9, tzinfo=UTC)
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


async def section(which: Section, payload) -> tuple:
    transport = Answer(payload)
    provider = CheckoCompanyDataProvider(
        KEY,
        Answer(),
        lambda: NOW,
        courts_transport=transport,
        fedresurs_transport=transport,
        enforcements_transport=transport,
    )
    (snapshot,) = await provider.fetch(CompanyDataRequest(INN, (which,)))
    return snapshot, transport


def kinds(snapshot, kind: FactKind) -> list:
    return [fact for fact in snapshot.facts if fact.kind is kind]


# --- arbitration cases ---


async def test_the_claim_load_comes_from_the_sources_own_totals():
    payload = fixture("courts_doc_shape")
    payload["data"]["Записи"] = payload["data"]["Записи"][:2]  # both with a readable date
    payload["data"]["ЗапВсего"] = 2
    snapshot, transport = await section(Section.COURTS, payload)
    assert transport.calls == [(KEY, INN, 20)]
    assert snapshot.status is FetchStatus.OK and snapshot.coverage is Coverage.COMPLETE
    assert [f.value for f in kinds(snapshot, FactKind.COURT_CASES_ACTIVE)] == [2]
    assert [f.value for f in kinds(snapshot, FactKind.COURT_CLAIMS_TOTAL)] == [Decimal("3600000")]
    cases = kinds(snapshot, FactKind.COURT_CASE)
    assert [f.observed_on for f in cases] == [date(2026, 9, 24), date(2026, 5, 10)]
    assert cases[0].value == (
        "А00-3/2026 · Арбитражный суд Синтетической области · иск 2 400 000 ₽ · "
        "истец: ООО «Синтетический кредитор»"
    )
    assert cases[1].value.endswith("истец: ФНС России и др.")
    # Every case links to its card in the arbitration file; the summary to the source.
    links = {item.id: item.url for item in snapshot.evidence}
    assert links[cases[0].evidence_ids[0]].startswith("https://kad.arbitr.ru/Card/")


async def test_a_busy_defendant_keeps_its_count_while_the_list_is_cut():
    payload = fixture("courts_doc_shape")
    payload["data"]["Записи"] = payload["data"]["Записи"][:2]
    payload["data"]["ЗапВсего"] = 140
    snapshot, _ = await section(Section.COURTS, payload)
    assert [f.value for f in kinds(snapshot, FactKind.COURT_CASES_ACTIVE)] == [140]
    assert snapshot.coverage is Coverage.PARTIAL
    assert snapshot.missing == (
        "Показаны последние 2 дел из 140; число дел и сумма исков — по всем.",
    )


async def test_a_case_without_a_readable_date_is_left_out_and_said():
    snapshot, _ = await section(Section.COURTS, fixture("courts_doc_shape"))
    assert len(kinds(snapshot, FactKind.COURT_CASE)) == 2
    assert "Часть дел без распознанной даты не показана." in snapshot.missing
    # A link that is not the source's https page is not kept.
    assert all(item.url is None or item.url.startswith("https://") for item in snapshot.evidence)


async def test_no_open_cases_is_a_complete_zero_not_a_missing_answer():
    snapshot, _ = await section(Section.COURTS, fixture("courts_empty"))
    assert snapshot.status is FetchStatus.OK and snapshot.coverage is Coverage.COMPLETE
    assert [f.value for f in kinds(snapshot, FactKind.COURT_CASES_ACTIVE)] == [0]
    assert not kinds(snapshot, FactKind.COURT_CASE)


@pytest.mark.parametrize("field,value", [("ЗапВсего", "много"), ("ЗапВсего", -1), ("Записи", {})])
async def test_a_changed_structure_is_never_read_as_no_cases(field, value):
    payload = fixture("courts_doc_shape")
    payload["data"][field] = value
    snapshot, _ = await section(Section.COURTS, payload)
    assert snapshot.status is FetchStatus.INVALID_RESPONSE
    assert snapshot.coverage is Coverage.UNAVAILABLE


# --- creditors' notices on Fedresurs ---


async def test_a_creditors_notice_is_a_fact_and_the_companys_own_is_not():
    snapshot, _ = await section(Section.FEDRESURS, fixture("fedresurs_doc_shape"))
    intents = kinds(snapshot, FactKind.BANKRUPTCY_INTENT)
    filings = kinds(snapshot, FactKind.BANKRUPTCY_FILING)
    # Five records: a creditor's notice against the company; the company's own notice
    # against someone else (publisher = its ОГРН); a pledge; the tax authority's filing
    # with the company as the participant; a filing about someone else. Two count.
    assert [f.observed_on for f in intents] == [date(2026, 3, 20)]
    assert [f.observed_on for f in filings] == [date(2026, 4, 10)]
    assert filings[0].value.startswith("Направление в арбитражный суд заявления уполномоченного")
    assert intents[0].value == "Намерение кредитора обратиться в суд с заявлением о банкротстве"
    link = {item.id: item.url for item in snapshot.evidence}[intents[0].evidence_ids[0]]
    assert link.startswith("https://fedresurs.ru/")
    assert snapshot.coverage is Coverage.COMPLETE


async def test_without_the_companys_ogrn_the_publisher_is_said_to_be_unchecked():
    payload = fixture("fedresurs_doc_shape")
    del payload["company"]
    snapshot, _ = await section(Section.FEDRESURS, payload)
    # Neither the publisher nor the participants can be matched by ОГРН: every notice of
    # the two types stays rather than none — the gap is said, not read as «nothing found».
    assert len(kinds(snapshot, FactKind.BANKRUPTCY_INTENT)) == 2
    assert len(kinds(snapshot, FactKind.BANKRUPTCY_FILING)) == 2
    assert snapshot.coverage is Coverage.PARTIAL
    assert "кто публикатор и участник — не сверено" in snapshot.missing[-1]


async def test_the_requests_ask_for_open_cases_as_defendant_and_every_fedresurs_type(
    monkeypatch,
):
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
            sent.append((url, kwargs["json"], kwargs["allow_redirects"]))
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(courts.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(fedresurs.aiohttp, "ClientSession", Session)
    await courts.AiohttpCourtsTransport().request(KEY, INN, 2)
    await fedresurs.AiohttpFedresursTransport().request(KEY, INN, 2)
    assert sent == [
        (
            courts.COURTS_URL,
            {
                "key": KEY,
                "inn": INN,
                "role": "defendant",
                "active": "true",
                "sort": "-date",
                "limit": 100,
            },
            False,
        ),
        (
            fedresurs.FEDRESURS_URL,
            {
                "key": KEY,
                "inn": INN,
                "sort": "-date",
                "limit": 100,
            },
            False,
        ),
    ]


# --- the rule ---


async def snapshots_with_intent(published_on: str) -> list:
    """A creditor's intention and nothing filed yet."""
    payload = fixture("fedresurs_doc_shape")
    payload["data"]["Записи"] = [
        r for r in payload["data"]["Записи"] if r["Тип"] == "CreditorIntentionGoToCourt"
    ]
    payload["data"]["ЗапВсего"] = len(payload["data"]["Записи"])
    payload["data"]["Записи"][0]["Дата"] = published_on
    demo = await DemoCompanyDataProvider().fetch(CompanyDataRequest(INN))
    live, _ = await section(Section.FEDRESURS, payload)
    return [s for s in demo if s.section is not Section.FEDRESURS] + [
        replace(live, mode=demo[0].mode)
    ]


async def test_a_recent_creditors_notice_makes_the_company_high():
    """The customer's case: EFRSB empty, a creditor's notice on Fedresurs — a high, named."""
    assessment = assess(INN, await snapshots_with_intent("2026-03-20"), analysis_date=NOW.date())
    signal = next(s for s in assessment.signals if s.code == "bankruptcy_intent")
    assert signal.level is Priority.HIGH
    assert assessment.priority is Priority.HIGH
    assert "20.03.2026" in signal.reason and "Федресурс" in signal.reason


async def test_a_notice_older_than_a_year_is_history():
    old = date.fromordinal(NOW.date().toordinal() - INTENT_WINDOW_DAYS - 1).isoformat()
    assessment = assess(INN, await snapshots_with_intent(old), analysis_date=NOW.date())
    assert "bankruptcy_intent" not in {s.code for s in assessment.signals}


async def test_the_card_has_no_analysis_date_and_counts_from_the_day_of_the_answer():
    """For the card «today» is the day the source answered (it is 15.10.2026 here)."""
    assessment = assess(INN, await snapshots_with_intent("2026-03-20"))
    assert "bankruptcy_intent" in {s.code for s in assessment.signals}


# --- the demo source answers every section ---


async def test_the_demo_source_answers_the_new_sections():
    snapshots = await DemoCompanyDataProvider(DemoScenario.ALARM).fetch(CompanyDataRequest(INN))
    by_section = {s.section: s for s in snapshots}
    assert set(by_section) == set(Section)
    assert [f.value for f in kinds(by_section[Section.COURTS], FactKind.COURT_CASES_ACTIVE)] == [2]
    assert not by_section[Section.FEDRESURS].facts
    # The court figures raise nothing by themselves: no agreed threshold yet.
    assessment = assess(INN, snapshots)
    assert {s.code for s in assessment.signals} == {"unresolved_bankruptcy_event"}


def test_the_provider_knows_every_section():
    """A section added to the contract without an adapter would fail every live card."""
    provider = checko.CheckoCompanyDataProvider(KEY, Answer())
    assert {"_courts", "_fedresurs"} <= set(dir(provider))


async def test_the_brief_card_names_the_claim_load_and_the_notice():
    """What the customer asked to see first (15.10.2026): the bankruptcy notice in the
    first line, and the claim load in one line, without opening the details."""
    from claims_assistant.application.check_company import CompanyCheck
    from claims_assistant.application.internal_context import card_assessment
    from claims_assistant.presentation.telegram.card import format_brief

    snapshots = await snapshots_with_intent("2026-03-20")
    alarm = await DemoCompanyDataProvider(DemoScenario.ALARM).fetch(
        CompanyDataRequest(INN, (Section.COURTS,))
    )
    snapshots = [s for s in snapshots if s.section is not Section.COURTS] + list(alarm)
    check = CompanyCheck(INN, tuple(snapshots))
    brief = format_brief(check, card_assessment(check, None))
    lines = brief.splitlines()
    assert lines[2] == "❗️ Высокий — требует внимания (по внешним данным)"
    assert "• Кредитор объявил на Федресурсе о намерении подать заявление о банкротстве" in brief
    assert (
        "Арбитраж: незавершённых дел, где организация ответчик, — 2, иски на 3 400 000,00 ₽."
        in lines
    )


# --- the filing in court (live check of 15.10.2026) ---


def test_legal_forms_are_shortened_in_the_case_line():
    assert courts.short_name('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТЕСТ"') == 'ООО "ТЕСТ"'
    assert courts.short_name("НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «Х»") == "АО «Х»"
    assert courts.short_name("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «Х»") == "ПАО «Х»"
    assert (
        courts.short_name("МЕЖРАЙОННАЯ ИНСПЕКЦИЯ ФЕДЕРАЛЬНОЙ НАЛОГОВОЙ СЛУЖБЫ № 0 ПО ОБЛАСТИ")
        == "МИФНС № 0 ПО ОБЛАСТИ"
    )
    assert courts.short_name("ООО «Уже коротко»") == "ООО «Уже коротко»"


async def test_a_bankruptcy_case_of_the_company_is_its_own_fact():
    """The customer's debtor: an open case of the bankruptcy kind, the tax authority filed,
    the company among the defendants. The other «Б» case — someone else's bankruptcy, where
    the company is not the debtor — stays an ordinary case."""
    snapshot, _ = await section(Section.COURTS, fixture("courts_bankruptcy_live_shape"))
    assert snapshot.status is FetchStatus.OK and snapshot.coverage is Coverage.COMPLETE
    (case,) = kinds(snapshot, FactKind.BANKRUPTCY_CASE)
    assert case.observed_on == date(2026, 4, 10)
    assert case.value == (
        "дело о банкротстве А00-1/2026 · АС Синтетической области · требование 1 000 000 ₽ · "
        "заявитель: МИФНС № 0 ПО СИНТЕТИЧЕСКОЙ ОБЛАСТИ"
    )
    (other,) = kinds(snapshot, FactKind.COURT_CASE)
    assert other.value.startswith("А00-9/2026")


async def customers_debtor() -> list:
    """EFRSB empty; in court a bankruptcy case filed by the tax authority; on Fedresurs its
    notice of the filing and an earlier creditor's intention — what A saw live."""
    demo = await DemoCompanyDataProvider().fetch(CompanyDataRequest(INN))
    courts_live, _ = await section(Section.COURTS, fixture("courts_bankruptcy_live_shape"))
    fedresurs_live, _ = await section(Section.FEDRESURS, fixture("fedresurs_doc_shape"))
    live = {Section.COURTS: courts_live, Section.FEDRESURS: fedresurs_live}
    return [replace(live[s.section], mode=s.mode) if s.section in live else s for s in demo]


async def test_the_filing_is_one_high_reason_with_both_grounds():
    assessment = assess(INN, await customers_debtor(), analysis_date=NOW.date())
    codes = [s.code for s in assessment.signals]
    # One reason, not two: the creditor's earlier intention adds nothing once filed.
    assert codes == ["bankruptcy_filed"]
    (signal,) = assessment.signals
    assert signal.level is Priority.HIGH and assessment.priority is Priority.HIGH
    assert signal.reason == (
        "В арбитражном суде заявление о признании организации банкротом (заявление от 10.04.2026)."
    )
    assert {fact_id.split("-")[0] for fact_id in signal.fact_ids} == {"court", "fedresurs"}


async def test_the_court_case_alone_is_enough():
    """Whoever filed: the case in court raises it even with nothing on Fedresurs."""
    snapshots = [s for s in await customers_debtor() if s.section is not Section.FEDRESURS]
    assessment = assess(INN, snapshots, analysis_date=NOW.date())
    assert [s.code for s in assessment.signals] == ["bankruptcy_filed"]


CHECK_THE_EVENT = "Проверить событие и документы по указанному источнику."


async def test_a_filing_asks_to_check_the_case_not_to_work_as_usual():
    """Review of S8-01: a high with a bankruptcy filing said «Работать в обычном порядке»
    in the report's «Рекомендация», the template explanation and the AI context."""
    assessment = assess(INN, await customers_debtor(), analysis_date=NOW.date())
    assert assessment.next_step == CHECK_THE_EVENT


async def test_a_creditors_notice_asks_to_check_it_not_to_work_as_usual():
    assessment = assess(INN, await snapshots_with_intent("2026-03-20"), analysis_date=NOW.date())
    assert [s.code for s in assessment.signals] == ["bankruptcy_intent"]
    assert assessment.next_step == CHECK_THE_EVENT


@pytest.mark.parametrize("code", ["bankruptcy_filed", "bankruptcy_intent"])
def test_a_bankruptcy_signal_outranks_overdue_in_the_next_step(code):
    """With an overdue as well, the court case is still the first thing to look at."""
    signals = (
        Signal(code, Priority.HIGH, "Банкротство."),
        Signal("overdue_60", Priority.HIGH, "Просрочка."),
    )
    assessment = Assessment(INN, Priority.HIGH, signals, (), {}, True)
    assert assessment.next_step == CHECK_THE_EVENT


async def test_an_old_filing_notice_without_a_case_is_history():
    payload = fixture("fedresurs_doc_shape")
    payload["data"]["Записи"] = [
        r for r in payload["data"]["Записи"] if r["Тип"] == "DebtorBankruptcyCourtNotification"
    ]
    payload["data"]["ЗапВсего"] = len(payload["data"]["Записи"])
    snapshot, _ = await section(Section.FEDRESURS, payload)
    demo = await DemoCompanyDataProvider().fetch(CompanyDataRequest(INN))
    snapshots = [
        replace(snapshot, mode=s.mode) if s.section is Section.FEDRESURS else s for s in demo
    ]
    later = date(2027, 6, 1)  # more than a year after 10.04.2026
    assert "bankruptcy_filed" not in {
        s.code for s in assess(INN, snapshots, analysis_date=later).signals
    }
    assert "bankruptcy_filed" in {
        s.code for s in assess(INN, snapshots, analysis_date=NOW.date()).signals
    }


async def test_the_brief_card_of_the_customers_debtor_says_bankruptcy_first():
    """The complaint of 15.10.2026, answered in the first lines of the card."""
    from claims_assistant.application.check_company import CompanyCheck
    from claims_assistant.application.internal_context import card_assessment
    from claims_assistant.presentation.telegram.card import format_brief, format_card

    check = CompanyCheck(INN, tuple(await customers_debtor()))
    brief = format_brief(check, card_assessment(check, None))
    lines = brief.splitlines()
    assert lines[2] == "❗️ Высокий — требует внимания (по внешним данным)"
    bullets = [line for line in lines if line.startswith("• ")]
    assert bullets[0] == (
        "• В арбитражном суде заявление о признании организации банкротом "
        "(заявление от 10.04.2026)."
    )
    full = format_card(check, None)
    assert "Дело о банкротстве: дело о банкротстве А00-1/2026" in full
