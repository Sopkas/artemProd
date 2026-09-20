"""Step payloads round-trip through JSON with exact domain types.

Snapshots go through B's domain/serialization (tested there); here the pipeline's
per-INN list and the import payload are checked end to end."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.step_payloads import (
    ImportedPackage,
    PayloadError,
    dump_import,
    dump_snapshots,
    load_import,
    load_snapshots,
)
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
    Period,
    ProviderError,
    Section,
)
from claims_assistant.domain.imports import ImportIssue, IssueSeverity

INN = "1234567894"
NOW = datetime(2026, 9, 19, 10, 30, tzinfo=UTC)
EVIDENCE = Evidence(id="e1", source="checko", record_id="r1", url="https://example.test/r1")


def fact(kind: FactKind, value, **extra) -> Fact:
    fact_id = f"{kind}-{type(value).__name__}"
    return Fact(id=fact_id, inn=INN, kind=kind, value=value, evidence_ids=("e1",), **extra)


def test_import_round_trip_keeps_decimals_dates_and_issue_coordinates():
    rows = (
        CounterpartyRow(
            inn=INN,
            name="ООО «Ромашка»",
            cutoff_date=date(2026, 9, 1),
            debt=Decimal("100.50"),
            overdue_days=12,
            last_payment_date=date(2026, 8, 1),
        ),
        CounterpartyRow(inn="7707083893"),
    )
    issues = (
        ImportIssue("empty_cell", IssueSeverity.WARNING, "Контрагенты", "Пусто", row=4, column="C"),
        ImportIssue("sheet_note", IssueSeverity.ERROR, "Контрагенты", "Нет листа", file_id="f1"),
    )
    restored = load_import(dump_import(ImportedPackage(rows, issues)))
    assert (restored.rows, restored.issues) == (rows, issues)
    assert type(restored.rows[0].debt) is Decimal and type(restored.rows[0].cutoff_date) is date


def test_snapshot_round_trip_keeps_every_value_type_and_the_error():
    ok = ExternalSnapshot(
        inn=INN,
        section=Section.FINANCES,
        source="checko",
        mode=DataMode.LIVE,
        fetched_at=NOW,
        status=FetchStatus.OK,
        coverage=Coverage.PARTIAL,
        facts=(
            fact(FactKind.COMPANY_NAME, "ООО «Ромашка»", observed_on=date(2026, 9, 1)),
            fact(FactKind.COMPANY_STATUS, CompanyStatus.LIQUIDATING, observed_on=date(2026, 9, 1)),
            fact(
                FactKind.REVENUE,
                Decimal("12000000.00"),
                period=Period(date(2025, 1, 1), date(2025, 12, 31)),
                unit="RUB",
            ),
            fact(FactKind.NET_PROFIT, 5, period=Period(date(2025, 1, 1), date(2025, 12, 31))),
            fact(FactKind.BANKRUPTCY_EVENT, True, observed_on=date(2026, 5, 5)),
            fact(FactKind.BANKRUPTCY_EVENT, date(2026, 5, 5), observed_on=date(2026, 5, 5)),
            Fact(
                id="missing",
                inn=INN,
                kind=FactKind.REVENUE,
                value=None,
                evidence_ids=("e1",),
                period=Period(date(2024, 1, 1), date(2024, 12, 31)),
                missing_reason="Нет отчётности за год.",
            ),
        ),
        evidence=(EVIDENCE,),
        missing=("Нет отчётности за 2024 год.",),
        covered_period=Period(date(2025, 1, 1), date(2025, 12, 31)),
        source_updated_at=datetime(2026, 9, 18, 0, 0, tzinfo=UTC),
    )
    failed = ExternalSnapshot(
        inn=INN,
        section=Section.BANKRUPTCY,
        source="checko",
        mode=DataMode.LIVE,
        fetched_at=NOW,
        status=FetchStatus.RATE_LIMITED,
        coverage=Coverage.UNAVAILABLE,
        missing=("Лимит запросов.",),
        error=ProviderError("rate_limited", "Лимит запросов.", retry_after_seconds=30.0),
    )
    restored = load_snapshots(dump_snapshots((ok, failed)))
    assert restored == (ok, failed)
    values = [f.value for f in restored[0].facts]
    assert [type(v) for v in values] == [str, CompanyStatus, Decimal, int, bool, date, type(None)]


@pytest.mark.parametrize(
    "payload",
    ["{not json", "[]", '{"rows": 1}', '[{"inn": "1"}]', '{"schema": 2, "rows": [], "issues": []}'],
)
def test_unreadable_import_payloads_raise_one_error_type(payload):
    with pytest.raises(PayloadError):
        load_import(payload)


@pytest.mark.parametrize("payload", ["{not json", "{}", '[{"inn": "1"}]', '[{"schema": 2}]'])
def test_unreadable_snapshot_payloads_raise_one_error_type(payload):
    with pytest.raises(PayloadError):
        load_snapshots(payload)


def test_payload_is_readable_json_without_escaped_cyrillic():
    rows = (CounterpartyRow(inn=INN, name="Ромашка"),)
    assert "Ромашка" in dump_import(ImportedPackage(rows, ()))


@pytest.mark.parametrize(
    "field, value",
    [
        ("debt", "abc"),  # Decimal raises InvalidOperation, an ArithmeticError
        ("overdue_days", True),  # bool is not an int
        ("inn", 1234567894),  # a number, not a string
        ("inn", "123"),  # not a 10-digit INN
        ("cutoff_date", 20260901),
        ("name", 5),
    ],
)
def test_import_payload_with_a_wrong_field_type_is_rejected(field, value):
    import json

    payload = json.loads(dump_import(ImportedPackage((CounterpartyRow(inn=INN),), ())))
    payload["rows"][0][field] = value
    with pytest.raises(PayloadError):
        load_import(json.dumps(payload))


@pytest.mark.parametrize("field, value", [("row", True), ("row", "4"), ("column", 3)])
def test_import_issue_with_a_wrong_field_type_is_rejected(field, value):
    import json

    issue = ImportIssue("empty_cell", IssueSeverity.WARNING, "Контрагенты", "Пусто", row=4)
    payload = json.loads(dump_import(ImportedPackage((), (issue,))))
    payload["issues"][0][field] = value
    with pytest.raises(PayloadError):
        load_import(json.dumps(payload))


def test_whole_package_round_trips_with_exact_types():
    """S4-04: the import step keeps every file, so a resumed run re-reads nothing."""
    from claims_assistant.domain.debt_history import DebtSnapshot
    from claims_assistant.domain.interactions import InteractionRow
    from claims_assistant.domain.payments import PaymentRow

    package = ImportedPackage(
        rows=(CounterpartyRow(inn=INN, debt=Decimal("10.00"), cutoff_date=date(2026, 9, 1)),),
        issues=(),
        payments=(PaymentRow(INN, "P-1", date(2026, 7, 15), Decimal("100.50")),),
        history=(DebtSnapshot(INN, date(2026, 8, 1), Decimal("0.00")),),
        interactions=(
            InteractionRow(INN, "I-1", date(2026, 8, 20), "Обещали оплатить.", "телефон"),
            InteractionRow(INN, "I-2", date(2026, 8, 21), "Без ответа.", None),
        ),
    )
    restored = load_import(dump_import(package))
    assert restored == package
    assert type(restored.payments[0].amount) is Decimal
    assert type(restored.history[0].debt) is Decimal


@pytest.mark.parametrize(
    "section, field, value",
    [
        ("payments", "amount", 100.5),
        ("payments", "paid_on", "15.07.2026"),
        ("payments", "payment_id", 1),
        ("history", "debt", "abc"),
        ("interactions", "comment", None),
        ("interactions", "channel", 5),
    ],
)
def test_optional_file_rows_with_wrong_types_are_rejected(section, field, value):
    import json

    from claims_assistant.domain.debt_history import DebtSnapshot
    from claims_assistant.domain.interactions import InteractionRow
    from claims_assistant.domain.payments import PaymentRow

    package = ImportedPackage(
        rows=(),
        issues=(),
        payments=(PaymentRow(INN, "P-1", date(2026, 7, 15), Decimal("1.00")),),
        history=(DebtSnapshot(INN, date(2026, 8, 1), Decimal("1.00")),),
        interactions=(InteractionRow(INN, "I-1", date(2026, 8, 20), "Текст.", None),),
    )
    payload = json.loads(dump_import(package))
    payload[section][0][field] = value
    with pytest.raises(PayloadError):
        load_import(json.dumps(payload))
