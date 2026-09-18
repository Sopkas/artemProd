from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.check_company import check_company
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
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram.card import format_card

INN = "1234567894"


async def card_for(scenario: DemoScenario) -> str:
    return format_card(await check_company(INN, DemoCompanyDataProvider(scenario)))


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
    assert text.count("— раздел недоступен") == 3
    assert text.count("Источник:") == 3
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
    assert text.count("Источник:") == 3
