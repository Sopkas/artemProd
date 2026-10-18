from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.check_company import CompanyCheck, check_company
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
    ProviderError,
    Section,
)
from claims_assistant.domain.scoring import Assessment, Priority, Signal, combine
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram.card import (
    _PART_TITLES,
    DETAILS_HINT,
    TELEGRAM_MESSAGE_LIMIT,
    format_brief,
    format_card,
    split_message,
    telegram_length,
)

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
    if scenario is DemoScenario.ERROR:  # every section failed, for one reason
        assert "Причина: Синтетический пример сбоя источника." in brief
    else:  # finances failed, the register answered in part: each says its own
        assert "  финансы — синтетический пример сбоя источника." in brief
        assert "  сообщения ЕФРСБ — ответ неполный, подробности — «Подробнее»." in brief


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


# --- S8-01 (A): the register by its name, one reason once, every section in Russian ---

LIMIT = "Достигнут лимит запросов Checko."


def live_failure(section: Section) -> ExternalSnapshot:
    """A section the live source could not read: its text is both the error and the gap."""
    return ExternalSnapshot(
        inn=INN,
        section=section,
        source="checko-company-v2",
        mode=DataMode.LIVE,
        fetched_at=datetime(2026, 10, 15, 6, 0, tzinfo=UTC),
        status=FetchStatus.UNAVAILABLE,
        coverage=Coverage.UNAVAILABLE,
        missing=(LIMIT,),
        error=ProviderError("rate_limited", LIMIT),
    )


def failed_check() -> CompanyCheck:
    return CompanyCheck(INN, tuple(live_failure(section) for section in Section))


def test_failed_section_says_its_reason_once():
    """The customer saw «Причина» and «Ограничение» with one text under every section."""
    card = format_card(failed_check())
    assert card.count(f"Причина: {LIMIT}") == len(Section)
    assert f"Ограничение: {LIMIT}" not in card


async def test_failed_section_keeps_a_limitation_of_its_own():
    """A gap that says something else than the error is still shown."""
    card = await card_for(DemoScenario.ERROR)
    assert "Причина: Синтетический пример сбоя источника." in card
    assert "Ограничение: Демонстрационный источник недоступен; раздел не проверен." in card


async def test_bankruptcy_section_is_named_by_its_register():
    """«Сообщения о банкротстве — не найдено» read as «no bankruptcy» while a case was in
    court (15.10.2026): the section says which register was read."""
    card = await card_for(DemoScenario.ORDINARY)
    assert "Сообщения ЕФРСБ — проверено полностью" in card
    assert "Сообщения о банкротстве" not in card


def test_every_section_is_named_in_russian_in_the_brief_card():
    """A section without a title showed its code («courts, fedresurs») in the gaps line."""
    check = failed_check()
    brief = format_brief(check, card_assessment(check, None))
    assert {section.value for section in Section} <= set(_PART_TITLES)
    gaps = next(line for line in brief.splitlines() if line.startswith("Проверено не полностью"))
    assert gaps == (
        "Проверено не полностью: организация, сообщения ЕФРСБ, финансы, "
        "арбитражные дела, Федресурс, ФССП."
    )


TIMEOUT = "Checko не ответил за отведённое время."
OUT_OF_TIME = "Лимит времени или запросов проверки исчерпан; раздел не проверен."


def answered(section: Section, coverage: Coverage = Coverage.COMPLETE) -> ExternalSnapshot:
    return replace(
        live_failure(section),
        status=FetchStatus.OK,
        coverage=coverage,
        missing=("Показаны последние 100 дел из 924.",) if coverage is Coverage.PARTIAL else (),
        error=None,
    )


def failing(section: Section, message: str) -> ExternalSnapshot:
    return replace(
        live_failure(section), missing=(message,), error=ProviderError("timeout", message)
    )


def gap_lines(*snapshots: ExternalSnapshot) -> list[str]:
    check = CompanyCheck(INN, snapshots)
    brief = format_brief(check, card_assessment(check, None))
    lines = brief.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith("Проверено не полностью"))
    end = next(n for n in range(start + 1, len(lines)) if not lines[n].startswith(("  ", "При")))
    return lines[start:end]


def test_one_reason_for_every_gap_stays_one_line():
    """18.10.2026 at the customer's: the register answered, five sections timed out — and
    the card said «лимит» for all five, the guard's word for the retry it had no time for."""
    snapshots = [failing(section, TIMEOUT) for section in Section]
    snapshots[1] = answered(Section.BANKRUPTCY)
    assert gap_lines(*snapshots) == [
        "Проверено не полностью: организация, финансы, арбитражные дела, Федресурс, ФССП.",
        f"Причина: {TIMEOUT}",
    ]


def test_each_reason_is_said_for_its_own_sections():
    """One «Причина» under sections that failed for different reasons, or under one that
    came in part, was wrong for some of them."""
    assert gap_lines(
        failing(Section.COMPANY, TIMEOUT),
        answered(Section.BANKRUPTCY),
        failing(Section.FINANCES, OUT_OF_TIME),
        answered(Section.COURTS, Coverage.PARTIAL),
        failing(Section.FEDRESURS, TIMEOUT),
        answered(Section.ENFORCEMENTS),
    ) == [
        "Проверено не полностью:",
        "  организация, Федресурс — Checko не ответил за отведённое время.",
        "  финансы — лимит времени или запросов проверки исчерпан; раздел не проверен.",
        "  арбитражные дела — ответ неполный, подробности — «Подробнее».",
    ]


# --- the full card is one Telegram message (live, 17.10.2026) ---


def _large_company() -> CompanyCheck:
    """A large defendant as Checko answers: the newest hundred cases and proceedings."""
    from claims_assistant.infrastructure.checko import courts, enforcements

    at = datetime(2026, 10, 16, tzinfo=UTC)
    cases = [
        {
            "Номер": f"А40-{n}/2026",
            "UUID": f"uuid-{n}",
            "СтрКАД": f"https://kad.arbitr.ru/Card/uuid-{n}",
            "Дата": f"2026-{1 + n % 9:02d}-{1 + n % 28:02d}",
            "Суд": "Арбитражный суд Синтетической области",
            "Ист": [{"ИНН": "0000001011", "Наим": "ООО «Синтетический кредитор с длинным именем»"}],
            "Ответ": [{"ИНН": INN, "Наим": "ООО «ТЕСТ»"}],
            "Тип": "Г",
            "СуммИск": 500_000 + n,
        }
        for n in range(100)
    ]
    proceedings = [
        {
            "ИспПрНомер": f"{n}/26/77006-ИП",
            "ИспПрДата": f"2026-{1 + n % 9:02d}-{1 + n % 28:02d}",
            "ПредмИсп": "Иные взыскания имущественного характера в пользу юридических лиц",
            "ОстЗадолж": 10_000.0 + n,
            "СудПристНаим": "Синтетический отдел судебных приставов",
        }
        for n in range(100)
    ]
    return CompanyCheck(
        INN,
        (
            courts.project_courts(INN, cases, 924, Decimal("8910159157.83"), at),
            enforcements.project_enforcements(INN, proceedings, 3, at),
        ),
    )


def test_a_large_company_fits_one_message_with_its_newest_records():
    """Live 17.10.2026: a hundred cases and proceedings made 23 708 characters and Telegram
    refused «Подробнее». Long lists show their newest five; the totals stay whole."""
    text = format_card(_large_company())
    assert telegram_length(text) <= TELEGRAM_MESSAGE_LIMIT
    assert text.count("  Дело: ") == 5 and text.count("  Производство: ") == 5
    # «Of the received»: the source itself returned a hundred of 924 (review B on #108).
    assert text.count("  …и ещё 95 из полученных; здесь — 5 новейших") == 2
    assert "Незавершённых дел: 924" in text  # the source's own total, not the five shown


def test_the_newest_records_are_the_ones_shown():
    check = _large_company()
    courts_snapshot = check.snapshots[0]
    cases = [fact for fact in courts_snapshot.facts if fact.kind is FactKind.COURT_CASE]
    newest = sorted(cases, key=lambda fact: fact.observed_on, reverse=True)[:5]
    text = format_card(check)
    assert all(str(fact.value) in text for fact in newest)
    oldest = min(cases, key=lambda fact: fact.observed_on)
    assert str(oldest.value) not in text


def test_a_long_text_is_split_at_line_ends_into_messages_telegram_takes():
    text = "\n".join(f"строка {n:04d} " + "x" * 90 for n in range(200))
    parts = split_message(text)
    assert len(parts) > 1 and all(len(part) <= TELEGRAM_MESSAGE_LIMIT for part in parts)
    assert "\n".join(parts) == text  # nothing lost, nothing added
    assert split_message("коротко") == ["коротко"]
    assert split_message("") == [""]
    huge = "y" * (2 * TELEGRAM_MESSAGE_LIMIT + 10)
    assert [len(part) for part in split_message(huge)] == [4096, 4096, 10]


def test_telegram_counts_an_emoji_as_two():
    """Review B on #108: Telegram counts UTF-16 units — 🔺 takes two, so 4096 Python
    characters of marks would be refused."""
    assert telegram_length("🔺 Выручка") == 10
    marks = "\n".join("🔺" * 90 for _ in range(40))  # 3639 characters, 7239 units
    parts = split_message(marks)
    assert len(parts) == 2 and all(telegram_length(p) <= TELEGRAM_MESSAGE_LIMIT for p in parts)
    assert [telegram_length(p) for p in split_message("🔺" * 3000)] == [4096, 1904]


def test_an_empty_line_at_the_joint_is_kept():
    """Review B on #108: a blank separator between sections fell out at a part's start."""
    text = "x" * TELEGRAM_MESSAGE_LIMIT + "\n\nРаздел"  # the blank line opens part two
    parts = split_message(text)
    assert "\n".join(parts) == text
    assert parts[-1] == "\nРаздел"
