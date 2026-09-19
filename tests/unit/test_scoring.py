"""S3-05: external rules and priority assembly — control examples from docs/scoring.md.

Snapshots come from the S3-04 projections, so these tests also pin the S3-04 → S3-05 seam.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.domain.counterparties import CounterpartyRow
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
from claims_assistant.domain.scoring import (
    RULES_VERSION,
    Assessment,
    Priority,
    Signal,
    assess,
    combine,
    report_order_key,
)
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import normalize_company

INN = "1234567894"
NOW = datetime(2026, 9, 18, tzinfo=UTC)
DAY = date(2026, 9, 1)
FULL_ROW = CounterpartyRow(
    inn=INN,
    cutoff_date=DAY,
    debt=Decimal("150000.00"),
    overdue_days=10,
    last_payment_date=date(2026, 8, 20),
)


def company(status_code="001", status_name="Действует"):
    payload = {
        "meta": {"status": "ok"},
        "data": {
            "ИНН": INN,
            "ОГРН": "0000000000000",
            "НаимСокр": "ДЕМО",
            "ДатаВып": "2026-09-01",
            "Статус": {"Код": status_code, "Наим": status_name},
        },
    }
    return normalize_company(payload, INN, NOW)


def company_with_status(status: CompanyStatus):
    evidence = Evidence("company-record", "test", "0000000000000")
    return ExternalSnapshot(
        inn=INN,
        section=Section.COMPANY,
        source="test",
        mode=DataMode.LIVE,
        fetched_at=NOW,
        status=FetchStatus.OK,
        coverage=Coverage.PARTIAL,
        facts=(
            Fact(
                "company-status",
                INN,
                FactKind.COMPANY_STATUS,
                status,
                (evidence.id,),
                observed_on=DAY,
            ),
        ),
        evidence=(evidence,),
        missing=("Получены только статус.",),
    )


def efrsb(records=(), complete=True):
    return bankruptcy.project_bankruptcy(INN, list(records), NOW, complete=complete, unreadable=0)


def fin(years):
    return finances.project_finances(INN, years, NOW)


def unavailable(section):
    return ExternalSnapshot(
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


CLEAN_FINANCES = {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 1100, "2400": 20}}


def clean_external():
    return [company(), efrsb(), fin(CLEAN_FINANCES)]


# --- control examples (docs/scoring.md) ---


def test_checko_unavailable_and_no_internal_data_is_unknown():
    result = assess(INN, [unavailable(section) for section in Section])
    assert result.priority is Priority.UNKNOWN
    assert result.signals == ()
    assert all(result.coverage[s.value] is Coverage.UNAVAILABLE for s in Section)
    assert result.coverage["internal"] is Coverage.UNAVAILABLE
    assert result.missing_data  # every gap is named, not silently zero


def test_revenue_drop_of_exactly_30_percent_is_medium():
    years = {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 700, "2400": 5}}
    result = assess(INN, [company(), efrsb(), fin(years)], FULL_ROW)
    assert [s.code for s in result.signals] == ["revenue_drop_30"]
    assert result.priority is Priority.MEDIUM
    assert result.signals[0].fact_ids == ("revenue-2024", "revenue-2025")


def test_loss_and_revenue_drop_are_two_medium_signals_so_high():
    years = {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 700, "2400": -5}}
    result = assess(INN, [company(), efrsb(), fin(years)], FULL_ROW)
    assert {s.code for s in result.signals} == {"revenue_drop_30", "net_loss"}
    assert result.priority is Priority.HIGH


def test_only_a_large_debt_with_a_complete_base_set_is_low():
    row = CounterpartyRow(
        inn=INN,
        cutoff_date=DAY,
        debt=Decimal("99000000.00"),
        overdue_days=0,
        last_payment_date=date(2026, 8, 30),
    )
    result = assess(INN, clean_external(), row)
    assert result.base_complete is True
    assert result.signals == ()
    assert result.priority is Priority.LOW


def test_missing_financial_statements_without_signals_is_unknown():
    result = assess(INN, [company(), efrsb(), fin({})], FULL_ROW)
    assert result.priority is Priority.UNKNOWN
    assert any("Финансовая отчётность" in reason for reason in result.missing_data)


def test_known_high_signal_is_not_lowered_by_gaps():
    # «Просрочка 90 дней, Checko недоступен → high»: the internal signal arrives in S4-07.
    overdue = Signal("overdue_60", Priority.HIGH, "Просрочка больше 60 дней.", value="90")
    result = assess(INN, [unavailable(s) for s in Section], None, internal_signals=[overdue])
    assert result.priority is Priority.HIGH
    assert result.base_complete is False


# --- external rules ---


def test_bankruptcy_messages_are_one_review_signal_not_a_confirmed_procedure():
    records = [
        {"Дата": "2026-05-01", "Тип": "Введение наблюдения", "Номер": "A"},
        {"Дата": "2026-06-01", "Тип": "Введение наблюдения", "Номер": "B"},
    ]
    result = assess(INN, [company(), efrsb(records), fin(CLEAN_FINANCES)], FULL_ROW)
    (signal,) = result.signals
    assert signal.code == "unresolved_bankruptcy_event"
    assert signal.level is Priority.HIGH
    assert signal.fact_ids == ("efrsb-event-0", "efrsb-event-1")
    assert signal.observed_on == date(2026, 6, 1)
    assert result.priority is Priority.HIGH
    assert "Проверить событие" in result.next_step


@pytest.mark.parametrize("status", [CompanyStatus.LIQUIDATING, CompanyStatus.LIQUIDATED])
def test_liquidation_status_is_critical(status):
    result = assess(INN, [company_with_status(status), efrsb(), fin(CLEAN_FINANCES)], FULL_ROW)
    assert [s.code for s in result.signals] == ["liquidation_status"]
    assert result.priority is Priority.CRITICAL


def test_unknown_company_status_is_neither_active_nor_liquidation():
    result = assess(INN, [company("000", "Не действует"), efrsb(), fin(CLEAN_FINANCES)], FULL_ROW)
    assert result.signals == ()
    # Status unknown → the company part of the base set is not confirmed.
    assert result.base_complete is False
    assert result.priority is Priority.UNKNOWN


@pytest.mark.parametrize(
    "years",
    [
        {2024: {"2110": 0}, 2025: {"2110": 100}},  # zero base: no percent change
        {2024: {"2110": -50}, 2025: {"2110": -100}},  # negative base
        {2022: {"2110": 1000}, 2025: {"2110": 100}},  # years not consecutive
    ],
)
def test_revenue_drop_needs_a_positive_consecutive_base(years):
    result = assess(INN, [company(), efrsb(), fin(years)], FULL_ROW)
    assert "revenue_drop_30" not in {s.code for s in result.signals}


def test_net_loss_uses_the_latest_available_year_only():
    years = {2024: {"2110": 10, "2400": -1}, 2025: {"2110": 10, "2400": 5}}
    result = assess(INN, [company(), efrsb(), fin(years)], FULL_ROW)
    assert "net_loss" not in {s.code for s in result.signals}


# --- base set and gaps ---


def test_missing_internal_values_are_not_zeros():
    row = CounterpartyRow(inn=INN)  # INN only: debt and overdue unknown, not 0
    result = assess(INN, clean_external(), row)
    assert result.base_complete is False
    assert result.priority is Priority.UNKNOWN
    assert result.coverage["internal"] is Coverage.UNAVAILABLE
    assert any("долга" in reason for reason in result.missing_data)


def test_incomplete_efrsb_sample_blocks_the_base_set():
    result = assess(INN, [company(), efrsb(complete=False), fin(CLEAN_FINANCES)], FULL_ROW)
    assert result.base_complete is False
    assert result.priority is Priority.UNKNOWN


def test_assessment_carries_rules_version_and_rejects_foreign_snapshots():
    result = assess(INN, clean_external(), FULL_ROW)
    assert result.rules_version == RULES_VERSION
    other = ExternalSnapshot(
        inn="7707083893",
        section=Section.COMPANY,
        source="test",
        mode=DataMode.LIVE,
        fetched_at=NOW,
        status=FetchStatus.UNAVAILABLE,
        coverage=Coverage.UNAVAILABLE,
        missing=("x",),
        error=ProviderError("x", "x"),
    )
    with pytest.raises(ValueError):
        assess(INN, [other])


# --- assembly and ordering ---


def test_combine_rule_table():
    medium_a = Signal("net_loss", Priority.MEDIUM, "a")
    medium_b = Signal("revenue_drop_30", Priority.MEDIUM, "b")
    assert combine([], base_complete=True) is Priority.LOW
    assert combine([], base_complete=False) is Priority.UNKNOWN
    assert combine([medium_a], base_complete=False) is Priority.MEDIUM
    assert combine([medium_a, medium_a], base_complete=True) is Priority.MEDIUM  # same rule
    assert combine([medium_a, medium_b], base_complete=True) is Priority.HIGH
    critical = Signal("liquidation_status", Priority.CRITICAL, "c")
    assert combine([medium_a, critical], base_complete=False) is Priority.CRITICAL


def test_signals_cannot_be_low_or_unknown():
    with pytest.raises(ValueError):
        Signal("x", Priority.LOW, "reason")


def test_report_order_is_rank_then_debt_then_overdue_then_inn():
    def item(priority, inn, debt, overdue):
        result = Assessment(inn, priority, (), (), {}, base_complete=False)
        return result, CounterpartyRow(inn=inn, debt=debt, overdue_days=overdue)

    rows = [
        item(Priority.LOW, "1000000009", Decimal("900"), 0),
        item(Priority.UNKNOWN, "1000000008", None, None),
        item(Priority.HIGH, "1000000007", Decimal("10"), 5),
        item(Priority.HIGH, "1000000006", Decimal("50"), 1),
        item(Priority.HIGH, "1000000005", None, 90),
        item(Priority.CRITICAL, "1000000004", Decimal("1"), 1),
        item(Priority.MEDIUM, "1000000003", Decimal("5"), 5),
    ]
    ordered = [a.inn for a, r in sorted(rows, key=lambda pair: report_order_key(*pair))]
    assert ordered == [
        "1000000004",  # critical
        "1000000006",  # high, debt 50
        "1000000007",  # high, debt 10
        "1000000005",  # high, unknown debt goes after known
        "1000000003",  # medium
        "1000000008",  # unknown
        "1000000009",  # low last
    ]
