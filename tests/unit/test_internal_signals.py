"""S4-07: internal signals from the «Контрагенты» row and their thresholds (docs/scoring.md)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.scoring import (
    INTERNAL_DEBT,
    INTERNAL_LAST_PAYMENT,
    INTERNAL_OVERDUE,
    Priority,
    assess,
    payment_age_days,
    row_signals,
)
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import normalize_company

INN = "1234567894"
DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 18, tzinfo=UTC)


def row(overdue=0, paid_days_ago=10, debt=Decimal("1000.00"), cutoff=DAY):
    return CounterpartyRow(
        inn=INN,
        cutoff_date=cutoff,
        debt=debt,
        overdue_days=overdue,
        last_payment_date=None if paid_days_ago is None else DAY - timedelta(days=paid_days_ago),
    )


def codes(line, analysis_date=DAY):
    return [s.code for s in row_signals(line, analysis_date)]


def clean_external(years=None):
    company = normalize_company(
        {
            "meta": {"status": "ok"},
            "data": {
                "ИНН": INN,
                "ОГРН": "0000000000000",
                "НаимСокр": "ДЕМО",
                "ДатаВып": "2026-09-01",
                "Статус": {"Код": "001", "Наим": "Действует"},
            },
        },
        INN,
        NOW,
    )
    efrsb = bankruptcy.project_bankruptcy(INN, [], NOW, complete=True, unreadable=0)
    fin = finances.project_finances(
        INN, years or {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 1100, "2400": 20}}, NOW
    )
    return [company, efrsb, fin]


def unavailable():
    return [
        ExternalSnapshot(
            inn=INN,
            section=section,
            source="test",
            mode=DataMode.LIVE,
            fetched_at=NOW,
            status=FetchStatus.UNAVAILABLE,
            coverage=Coverage.UNAVAILABLE,
            missing=("Источник недоступен.",),
            error=ProviderError("network_error", "Источник недоступен."),
        )
        for section in Section
    ]


# --- thresholds ---


@pytest.mark.parametrize(
    "overdue,expected",
    [
        (None, []),
        (0, []),
        (30, []),
        (31, ["overdue_30"]),
        (60, ["overdue_30"]),
        (61, ["overdue_60"]),
        (90, ["overdue_60"]),  # mutually exclusive: never both
    ],
)
def test_overdue_thresholds(overdue, expected):
    assert codes(row(overdue=overdue)) == expected


@pytest.mark.parametrize(
    "paid_days_ago,expected",
    [(None, []), (0, []), (60, []), (61, ["no_payments_60"]), (200, ["no_payments_60"])],
)
def test_payment_age_threshold(paid_days_ago, expected):
    assert codes(row(paid_days_ago=paid_days_ago)) == expected


@pytest.mark.parametrize("debt", [Decimal("0"), None])
def test_old_payment_without_a_positive_known_debt_is_not_a_signal(debt):
    assert codes(row(paid_days_ago=120, debt=debt)) == []


def test_payment_after_the_analysis_date_is_a_contradiction_not_an_age():
    line = CounterpartyRow(inn=INN, debt=Decimal("5"), last_payment_date=DAY + timedelta(days=3))
    assert payment_age_days(line, DAY) is None
    assert codes(line) == []


def test_payment_age_needs_an_analysis_date():
    line = row(paid_days_ago=120, cutoff=None, overdue=None)
    assert codes(line, analysis_date=None) == []
    assert codes(line, analysis_date=DAY) == ["no_payments_60"]


def test_signals_cite_internal_fact_ids_and_values():
    (overdue,) = row_signals(row(overdue=75), DAY)
    assert overdue.level is Priority.HIGH
    assert overdue.fact_ids == (INTERNAL_OVERDUE,)
    assert overdue.value == "75" and overdue.observed_on == DAY
    (payment,) = row_signals(row(paid_days_ago=90), DAY)
    assert payment.fact_ids == (INTERNAL_LAST_PAYMENT, INTERNAL_DEBT)
    assert payment.value == "90"


# --- control examples through the whole assembly (docs/scoring.md) ---


def test_overdue_exactly_60_days_without_other_signals_is_medium():
    result = assess(INN, clean_external(), row(overdue=60))
    assert [s.code for s in result.signals] == ["overdue_30"]
    assert result.priority is Priority.MEDIUM


def test_overdue_61_days_is_high():
    result = assess(INN, clean_external(), row(overdue=61))
    assert result.priority is Priority.HIGH
    assert "Уточнить причины задержки" in result.next_step


def test_overdue_90_days_with_checko_unavailable_is_high_and_incomplete():
    result = assess(INN, unavailable(), row(overdue=90))
    assert result.priority is Priority.HIGH
    assert result.base_complete is False
    assert all(result.coverage[s.value] is Coverage.UNAVAILABLE for s in Section)


def test_two_different_medium_signals_make_high():
    result = assess(INN, clean_external(), row(overdue=45, paid_days_ago=90))
    assert {s.code for s in result.signals} == {"overdue_30", "no_payments_60"}
    assert result.priority is Priority.HIGH


def test_internal_and_external_medium_signals_combine():
    drop = {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 700, "2400": 5}}
    result = assess(INN, clean_external(drop), row(overdue=45))
    assert {s.code for s in result.signals} == {"overdue_30", "revenue_drop_30"}
    assert result.priority is Priority.HIGH


def test_no_internal_signal_and_a_complete_base_set_is_low():
    result = assess(INN, clean_external(), row(overdue=20, paid_days_ago=10))
    assert result.signals == ()
    assert result.priority is Priority.LOW


def test_explicit_analysis_date_overrides_the_cutoff():
    line = row(overdue=None, paid_days_ago=30)  # 30 days before the cut-off
    later = DAY + timedelta(days=40)  # 70 days after that payment
    assert assess(INN, clean_external(), line).signals == ()
    result = assess(INN, clean_external(), line, analysis_date=later)
    assert [s.code for s in result.signals] == ["no_payments_60"]
