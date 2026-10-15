from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.check_company import check_company
from claims_assistant.application.internal_context import card_assessment
from claims_assistant.domain.external import (
    CompanyStatus,
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
from claims_assistant.domain.scoring import Assessment, Priority, Signal, combine
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram.card import DETAILS_HINT, format_brief, format_card

INN = "1234567894"


async def card_for(scenario: DemoScenario) -> str:
    return format_card(await check_company(INN, DemoCompanyDataProvider(scenario)))


async def brief_for(scenario: DemoScenario) -> str:
    """The brief card of a company the user has no data of their own on."""
    check = await check_company(INN, DemoCompanyDataProvider(scenario))
    return format_brief(check, card_assessment(check, None))


def first_line(text: str) -> str:
    """The conclusion: the first line after the demo banner."""
    return text.splitlines()[2]


async def test_brief_card_says_the_point_first_and_is_much_shorter():
    """The customer's first remark (11.10.2026): less text, the conclusion at a glance."""
    brief = await brief_for(DemoScenario.ORDINARY)
    assert brief.startswith("ДЕМО")
    # ℹ️, not ✅: with no data of the user's own the check is not complete (review A on #86).
    assert first_line(brief) == "ℹ️ Во внешних данных значимых сигналов нет"
    assert "✅" not in brief
    assert f"ДЕМО — вымышленная компания, ИНН {INN}" in brief
    assert brief.endswith(DETAILS_HINT)
    assert "Источник:" not in brief and "synthetic-demo-v1" not in brief
    assert len(brief) * 2 < len(await card_for(DemoScenario.ORDINARY))


async def test_brief_card_names_the_reason_of_an_alarm():
    brief = await brief_for(DemoScenario.ALARM)
    # Without the user's own data the only alarm is the register's: it answers by INN,
    # whatever the company's role, so the card does not sound «высокий» on it (#86 review).
    assert first_line(brief) == "⚠️ Есть сообщения ЕФРСБ — роль организации не подтверждена"
    assert "❗️" not in brief
    assert "• Есть сообщения ЕФРСБ о банкротстве" in brief
    assert "Своих данных по этой организации нет" in brief


EFRSB = Signal("unresolved_bankruptcy_event", Priority.HIGH, "Есть сообщения ЕФРСБ.")
LIQUIDATION = Signal("liquidation_status", Priority.CRITICAL, "Статус — ликвидация.")
REVENUE = Signal("revenue_drop_30", Priority.MEDIUM, "Выручка упала на 40 %.")


async def external_only(*signals: Signal) -> str:
    """The brief card of a company without the owner's data, with these rules fired."""
    check = await check_company(INN, DemoCompanyDataProvider(DemoScenario.ORDINARY))
    found = card_assessment(check, None)
    assessment = Assessment(
        inn=INN,
        priority=combine(signals, base_complete=False),
        signals=signals,
        missing_data=found.missing_data,
        coverage=found.coverage,
        base_complete=False,
    )
    return first_line(format_brief(check, assessment))


@pytest.mark.parametrize(
    ("signals", "line"),
    [
        # Another external rule decides the mark; the register's messages stay a reason.
        ((EFRSB, LIQUIDATION), "❗️ Критичный — требует внимания (по внешним данным)"),
        ((EFRSB, REVENUE), "⚠️ Средний — есть основания присмотреться (по внешним данным)"),
        ((LIQUIDATION,), "❗️ Критичный — требует внимания (по внешним данным)"),
        ((EFRSB,), "⚠️ Есть сообщения ЕФРСБ — роль организации не подтверждена"),
    ],
)
async def test_without_own_data_the_register_alone_does_not_make_the_mark(signals, line):
    assert await external_only(*signals) == line


@pytest.mark.parametrize("scenario", [DemoScenario.INCOMPLETE, DemoScenario.ERROR])
async def test_brief_card_never_marks_an_incomplete_check_as_clean(scenario):
    """✅ is «checked in full, nothing found»: an unread section is ❔, with its reason."""
    brief = await brief_for(scenario)
    assert first_line(brief) == "❔ Внешние данные неполные"
    assert "✅" not in brief
    assert "Проверено не полностью:" in brief and "финансы" in brief
    assert "Причина: Синтетический пример сбоя источника." in brief


async def test_brief_card_does_not_rate_reliability_and_keeps_the_manual_check():
    """«Индекс благонадёжности» is not what the rules compute; and what the bot does not
    check at all stays said in the brief card, or ✅ would read as «blocks checked too»."""
    for scenario in DemoScenario:
        brief = await brief_for(scenario)
        assert "надёжн" not in brief.lower() and "индекс" not in brief.lower()
        assert "Блокировки счетов ФНС: бот не проверяет." in brief


async def test_ordinary_card_shows_inn_name_status_finances_and_demo_mark():
    text = await card_for(DemoScenario.ORDINARY)
    assert text.startswith("ДЕМО")
    assert f"ИНН {INN}" in text
    assert "ДЕМО — вымышленная компания" in text
    assert "действует" in text
    assert "12 000 000,00 ₽" in text
    assert "0,00 ₽" in text
    assert "2025" in text
    assert "synthetic-demo-v1" in text
    assert "01.01.2026" in text


async def test_ordinary_card_reports_empty_complete_bankruptcy_selection():
    text = await card_for(DemoScenario.ORDINARY)
    assert "сообщений не найдено" in text
    assert "Период: 2025" in text


async def test_alarm_card_shows_the_event():
    text = await card_for(DemoScenario.ALARM)
    assert "ДЕМО — сообщение о введении наблюдения" in text
    assert "31.12.2025" in text


async def test_incomplete_card_separates_partial_and_unavailable():
    text = await card_for(DemoScenario.INCOMPLETE)
    assert "неполные данные" in text
    assert "Демовыборка охватывает только декабрь" in text
    assert "01.12.2025 – 31.12.2025" in text
    assert "недоступен" in text
    assert "Синтетический пример сбоя источника" in text


async def test_error_card_marks_every_section_unavailable_and_promises_nothing():
    text = await card_for(DemoScenario.ERROR)
    assert text.count("— раздел недоступен") == len(Section)
    assert text.count("Источник:") == len(Section)
    assert "приоритет" not in text.lower()
    assert "риск" not in text.lower()


async def test_card_never_claims_a_priority():
    for scenario in DemoScenario:
        text = await card_for(scenario)
        assert "приоритет" not in text.lower()
        assert "Оценка" not in text


def test_unknown_fact_value_shows_the_reason():
    evidence = Evidence("e1", "test", "rec-1")
    snapshot = ExternalSnapshot(
        INN,
        Section.COMPANY,
        "test",
        DataMode.LIVE,
        datetime(2026, 3, 1, tzinfo=UTC),
        FetchStatus.OK,
        Coverage.COMPLETE,
        facts=(
            Fact(
                "f1",
                INN,
                FactKind.COMPANY_STATUS,
                None,
                ("e1",),
                observed_on=date(2026, 3, 1),
                missing_reason="Статус не сопоставлен",
            ),
        ),
        evidence=(evidence,),
    )
    from claims_assistant.application.check_company import CompanyCheck

    text = format_card(CompanyCheck(INN, (snapshot,)))
    assert not text.startswith("ДЕМО")
    assert "Статус не сопоставлен" in text
    assert "неизвестно" in text


@pytest.mark.parametrize(
    ("status", "label"),
    [
        (CompanyStatus.ACTIVE, "действует"),
        (CompanyStatus.LIQUIDATING, "в стадии ликвидации"),
        (CompanyStatus.LIQUIDATED, "ликвидирована"),
    ],
)
def test_company_status_is_translated(status, label):
    evidence = Evidence("e1", "test", "rec-1")
    snapshot = ExternalSnapshot(
        INN,
        Section.COMPANY,
        "test",
        DataMode.LIVE,
        datetime(2026, 3, 1, tzinfo=UTC),
        FetchStatus.OK,
        Coverage.COMPLETE,
        facts=(
            Fact("f1", INN, FactKind.COMPANY_STATUS, status, ("e1",), observed_on=date(2026, 3, 1)),
        ),
        evidence=(evidence,),
    )
    from claims_assistant.application.check_company import CompanyCheck

    assert label in format_card(CompanyCheck(INN, (snapshot,)))


def test_money_formatting_uses_russian_grouping_and_unit():
    evidence = Evidence("e1", "test", "rec-1")
    snapshot = ExternalSnapshot(
        INN,
        Section.FINANCES,
        "test",
        DataMode.LIVE,
        datetime(2026, 3, 1, tzinfo=UTC),
        FetchStatus.OK,
        Coverage.COMPLETE,
        covered_period=Period(date(2024, 1, 1), date(2024, 12, 31)),
        facts=(
            Fact(
                "f1",
                INN,
                FactKind.REVENUE,
                Decimal("1234567.5"),
                ("e1",),
                period=Period(date(2024, 1, 1), date(2024, 12, 31)),
                unit="RUB",
            ),
        ),
        evidence=(evidence,),
    )
    from claims_assistant.application.check_company import CompanyCheck

    text = format_card(CompanyCheck(INN, (snapshot,)))
    assert "1 234 567,50 ₽" in text
    assert "2024" in text


async def test_partial_empty_bankruptcy_is_not_reported_as_complete_absence():
    from claims_assistant.application.check_company import check_company
    from claims_assistant.infrastructure.demo.company_data import (
        DemoCompanyDataProvider,
        DemoScenario,
    )

    check = await check_company(INN, DemoCompanyDataProvider(DemoScenario.INCOMPLETE))
    text = format_card(check)
    assert "проверка неполная" in text
    assert "За проверенный период сообщений не найдено" not in text
    assert text.count("Источник:") == len(Section)


async def test_card_says_what_is_not_checked_and_where_to_look():
    """S7-03: the tax service hides account blocks behind a captcha. The card must not let
    their absence read as «no blocks»: it names them and links the page to check by hand."""
    for scenario in DemoScenario:
        text = await card_for(scenario)
        assert "Блокировки счетов ФНС: не проверяются автоматически" in text
        assert "https://service.nalog.ru/bi.do" in text
        assert "БИК любого банка" in text
