"""S8-04 (part A): a mark beside each figure in the brief card.

The customer asked (15.10.2026) to see at a glance whether a figure is good or bad —
«выручка снизилась → красный восклицательный знак». The direction comes from
``scoring.finance_trends``; a figure on which a rule fired gets that rule's priority mark,
so a line never says more or less than the first line of the card. Snapshots are built by
the finances adapter from line codes (2110 revenue, 2400 net profit).
"""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.check_company import CompanyCheck, check_company
from claims_assistant.application.internal_context import card_assessment
from claims_assistant.infrastructure.checko import finances
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram.card import _short_figure, format_brief
from claims_assistant.presentation.telegram.texts import PRIORITY_MARKS

INN = "1234567894"
NOW = datetime(2026, 10, 16, tzinfo=UTC)


def brief(years) -> str:
    check = CompanyCheck(INN, (finances.project_finances(INN, years, NOW),))
    return format_brief(check, card_assessment(check, None))


def finance_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "Выручка 2" in line or "прибыль 2" in line]


def test_the_customers_example_shows_both_directions_without_an_alarm():
    """15.10.2026: revenue +12 %, net profit 3,8 → 0,76 million — no rule fires on it."""
    text = brief(
        {
            2024: {"2110": 100_000_000, "2400": 3_800_000},
            2025: {"2110": 112_000_000, "2400": 760_000},
        }
    )
    assert finance_lines(text) == [
        "🔺 Выручка 2025: 112 млн ₽, +12 % к 2024",
        "🔻 Чистая прибыль 2025: 760 тыс. ₽, −80 % к 2024",
    ]


def test_a_revenue_drop_has_the_mark_of_its_rule_and_its_figure():
    years = {2024: {"2110": 1000}, 2025: {"2110": 645}}
    check = CompanyCheck(INN, (finances.project_finances(INN, years, NOW),))
    assessment = card_assessment(check, None)
    rule = next(s for s in assessment.signals if s.code == "revenue_drop_30")
    assert finance_lines(format_brief(check, assessment)) == [
        f"{PRIORITY_MARKS[rule.level]} Выручка 2025: 645 ₽, −35,5 % к 2024"
    ]


def test_a_fall_just_short_of_the_threshold_never_reads_as_the_threshold():
    """−29,996 % is no alarm; rounded it would print «−30 %» under a 🔻."""
    lines = finance_lines(brief({2024: {"2110": 100_000}, 2025: {"2110": 70_004}}))
    assert lines == ["🔻 Выручка 2025: 70 тыс. ₽, −29,9 % к 2024"]


def test_a_loss_has_the_mark_of_its_rule():
    years = {2024: {"2400": 1_200_000}, 2025: {"2400": -300_000}}
    check = CompanyCheck(INN, (finances.project_finances(INN, years, NOW),))
    assessment = card_assessment(check, None)
    rule = next(s for s in assessment.signals if s.code == "net_loss")
    assert finance_lines(format_brief(check, assessment)) == [
        f"{PRIORITY_MARKS[rule.level]} Чистая прибыль 2025: убыток 300 тыс. ₽, −125 % к 2024"
    ]


def test_after_a_loss_the_previous_year_is_named_instead_of_a_percent():
    lines = finance_lines(brief({2024: {"2400": -300_000}, 2025: {"2400": 1_500_000}}))
    assert lines == ["🔺 Чистая прибыль 2025: 1,5 млн ₽, в 2024 — убыток 300 тыс. ₽"]


def test_billions_are_short_too():
    lines = finance_lines(brief({2024: {"2110": 1_000_000_000}, 2025: {"2110": 1_260_000_000}}))
    assert lines == ["🔺 Выручка 2025: 1,3 млрд ₽, +26 % к 2024"]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        # Rounded at its own scale the figure would read «1 000 тыс.» / «1 000 млн» (B on #97).
        (Decimal("999950"), "1 млн ₽"),
        (Decimal("999500"), "1 млн ₽"),
        (Decimal("999960000"), "1 млрд ₽"),
        (Decimal("999.6"), "1 тыс. ₽"),
        (Decimal("-999950"), "убыток 1 млн ₽"),
        # Below the edge a figure keeps its own scale.
        (Decimal("999499"), "999 тыс. ₽"),
        (Decimal("950000"), "950 тыс. ₽"),
        (Decimal("999940000"), "999,9 млн ₽"),
        (Decimal("500"), "500 ₽"),
        (Decimal("0"), "0 ₽"),
        (Decimal("1234567000000"), "1 234,6 млрд ₽"),
    ],
)
def test_the_scale_is_chosen_after_rounding(value, text):
    assert _short_figure(value) == text


def test_a_figure_at_the_edge_of_a_scale_reads_as_the_next_one_in_the_card():
    lines = finance_lines(brief({2024: {"2110": 999_950}, 2025: {"2110": 999_950}}))
    assert lines == ["➖ Выручка 2025: 1 млн ₽, без изменений к 2024"]


def test_one_year_gives_no_line():
    assert finance_lines(brief({2025: {"2110": 112_000_000, "2400": 760_000}})) == []


async def test_demo_figures_are_flat_and_stand_between_the_reasons_and_own_data():
    check = await check_company(INN, DemoCompanyDataProvider(DemoScenario.ALARM))
    text = format_brief(check, card_assessment(check, None))
    lines = text.splitlines()
    revenue, profit = finance_lines(text)
    assert re.fullmatch(r"➖ Выручка (\d{4}): 12 млн ₽, без изменений к \d{4}", revenue)
    assert re.fullmatch(r"➖ Чистая прибыль (\d{4}): 0 ₽, без изменений к \d{4}", profit)
    reason = next(i for i, line in enumerate(lines) if line.startswith("• "))
    own = next(i for i, line in enumerate(lines) if line.startswith("Своих данных"))
    assert reason < lines.index(revenue) < own


async def test_no_lines_when_the_finances_are_not_there():
    check = await check_company(INN, DemoCompanyDataProvider(DemoScenario.INCOMPLETE))
    assert finance_lines(format_brief(check, card_assessment(check, None))) == []
