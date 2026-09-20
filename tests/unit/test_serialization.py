"""Domain results as JSON step payloads: exact round trips and strict reading."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.scoring import Priority, assess
from claims_assistant.domain.serialization import (
    SCHEMA,
    PayloadError,
    assessment_from_dict,
    assessment_to_dict,
    counterparty_row_from_dict,
    counterparty_row_to_dict,
    debt_snapshot_from_dict,
    debt_snapshot_to_dict,
    import_issue_from_dict,
    import_issue_to_dict,
    interaction_row_from_dict,
    interaction_row_to_dict,
    payment_row_from_dict,
    payment_row_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
)
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import normalize_company
from claims_assistant.infrastructure.demo.company_data import (
    DemoCompanyDataProvider,
    DemoScenario,
)

INN = "1234567894"
NOW = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)


def company(status=("001", "Действует")):
    payload = {
        "meta": {"status": "ok"},
        "data": {
            "ИНН": INN,
            "ОГРН": "0000000000000",
            "НаимСокр": "ДЕМО",
            "ДатаВып": "2026-09-01",
            "Статус": {"Код": status[0], "Наим": status[1]},
        },
    }
    return normalize_company(payload, INN, NOW)


def failed():
    return ExternalSnapshot(
        inn=INN,
        section=Section.FINANCES,
        source="checko-finances-v2",
        mode=DataMode.LIVE,
        fetched_at=NOW,
        status=FetchStatus.RATE_LIMITED,
        coverage=Coverage.UNAVAILABLE,
        missing=("Достигнут лимит запросов Checko.",),
        error=ProviderError("rate_limited", "Достигнут лимит запросов Checko.", 2.5),
    )


def through_json(data):
    """What storage does: a JSON string in, a JSON string out."""
    return json.loads(json.dumps(data, ensure_ascii=False))


LIVE_SNAPSHOTS = [
    company(),
    company(("000", "Не действует")),  # unknown status: None value with a reason
    bankruptcy.project_bankruptcy(
        INN,
        [{"Дата": "2026-05-01", "Тип": "Введение наблюдения", "Номер": "A-1"}],
        NOW,
        complete=True,
        unreadable=0,
    ),
    finances.project_finances(
        INN, {2024: {"2110": 1000, "2400": -50}, 2025: {"2110": "2 000,50", "2400": 75.5}}, NOW
    ),
    failed(),
]


@pytest.mark.parametrize("snapshot", LIVE_SNAPSHOTS, ids=lambda s: f"{s.section}-{s.status}")
def test_live_snapshots_round_trip_exactly_through_json(snapshot):
    assert snapshot_from_dict(through_json(snapshot_to_dict(snapshot))) == snapshot


@pytest.mark.parametrize("scenario", list(DemoScenario))
async def test_demo_snapshots_round_trip(scenario):
    snapshots = await DemoCompanyDataProvider(scenario).fetch(CompanyDataRequest(INN))
    for snapshot in snapshots:
        assert snapshot_from_dict(through_json(snapshot_to_dict(snapshot))) == snapshot


def test_values_keep_their_types():
    fin = LIVE_SNAPSHOTS[3]
    restored = snapshot_from_dict(through_json(snapshot_to_dict(fin)))
    values = {fact.id: fact.value for fact in restored.facts}
    assert values["revenue-2025"] == Decimal("2000.50")
    assert isinstance(values["revenue-2025"], Decimal)
    assert restored.fetched_at == NOW and restored.fetched_at.utcoffset() is not None
    assert restored.error is None
    err = snapshot_from_dict(through_json(snapshot_to_dict(failed()))).error
    assert err.retry_after_seconds == 2.5


def test_assessment_round_trips_with_signals_and_coverage():
    row = CounterpartyRow(
        inn=INN,
        cutoff_date=date(2026, 9, 1),
        debt=Decimal("1000.00"),
        overdue_days=75,
        last_payment_date=date(2026, 5, 1),
    )
    result = assess(INN, LIVE_SNAPSHOTS[:1] + LIVE_SNAPSHOTS[2:4], row)
    assert result.priority is Priority.HIGH and len(result.signals) >= 2
    restored = assessment_from_dict(through_json(assessment_to_dict(result)))
    assert restored == result
    assert hash(restored) == hash(result)
    assert restored.coverage["internal"] is Coverage.COMPLETE


def test_payload_is_plain_json_with_a_schema():
    data = snapshot_to_dict(LIVE_SNAPSHOTS[3])
    assert data["schema"] == SCHEMA
    json.dumps(data)  # no Decimal, date or enum objects left inside
    assert assessment_to_dict(assess(INN, [], None))["schema"] == SCHEMA


# --- strict reading ---


def mutated(change):
    data = through_json(snapshot_to_dict(LIVE_SNAPSHOTS[3]))
    change(data)
    return data


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema=2),
        lambda d: d.pop("schema"),
        lambda d: d.pop("fetched_at"),
        lambda d: d.update(fetched_at="2026-09-18T12:30:00"),  # naive timestamp
        lambda d: d.update(section="payments"),
        lambda d: d["facts"][0]["value"].update(type="float"),
        lambda d: d["facts"][0]["value"].update(value="много"),  # unreadable decimal
        lambda d: d["facts"][0].update(evidence_ids="finances-2024"),
        lambda d: d.update(coverage="complete", missing=["x"]),  # breaks a domain rule
        lambda d: d.update(inn=1234567894),
    ],
    ids=[
        "schema",
        "no-schema",
        "no-field",
        "naive",
        "enum",
        "value-type",
        "decimal",
        "list",
        "domain-rule",
        "inn-type",
    ],
)
def test_malformed_payload_is_rejected_without_echoing_it(change):
    data = mutated(change)
    with pytest.raises(PayloadError) as error:
        snapshot_from_dict(data)
    assert INN not in str(error.value)
    assert "много" not in str(error.value)


def test_bool_is_not_accepted_as_a_number_or_an_int_as_a_bool():
    data = through_json(snapshot_to_dict(failed()))
    data["error"]["retry_after_seconds"] = True
    with pytest.raises(PayloadError):
        snapshot_from_dict(data)
    assessment = through_json(assessment_to_dict(assess(INN, [], None)))
    bad = deepcopy(assessment)
    bad["base_complete"] = 1
    with pytest.raises(PayloadError):
        assessment_from_dict(bad)


# --- import step: «Контрагенты» rows and import issues ---

FULL = CounterpartyRow(
    inn=INN,
    name="ООО «Ромашка»",
    cutoff_date=date(2026, 9, 1),
    debt=Decimal("150000.50"),
    overdue_days=45,
    last_payment_date=date(2026, 7, 15),
)
ISSUE = ImportIssue(
    code="debt_negative",
    severity=IssueSeverity.ERROR,
    sheet="Контрагенты",
    reason="Сумма долга не может быть отрицательной.",
    row=7,
    column="D",
)


@pytest.mark.parametrize("row", [FULL, CounterpartyRow(inn=INN)], ids=["full", "inn-only"])
def test_counterparty_row_round_trips_exactly(row):
    assert counterparty_row_from_dict(through_json(counterparty_row_to_dict(row))) == row


def test_import_issue_round_trips_exactly():
    sheet_issue = ImportIssue("sheet_missing", IssueSeverity.ERROR, "Контрагенты", "Нет листа.")
    for issue in (ISSUE, sheet_issue):
        assert import_issue_from_dict(through_json(import_issue_to_dict(issue))) == issue


def test_reads_the_shape_the_s3_01_import_step_already_stores():
    # Literally what step_payloads wrote before the move: saved steps stay readable.
    stored = {
        "inn": INN,
        "name": None,
        "cutoff_date": "2026-09-01",
        "debt": "100.00",
        "overdue_days": 5,
        "last_payment_date": None,
    }
    row = counterparty_row_from_dict(stored)
    assert row.debt == Decimal("100.00") and isinstance(row.debt, Decimal)
    assert counterparty_row_to_dict(row) == stored


@pytest.mark.parametrize(
    "field,value",
    [
        ("inn", 1234567894),  # a number, not a string
        ("inn", "1234567890"),  # ten digits, wrong control digit
        ("debt", "abc"),  # InvalidOperation, not ValueError
        ("debt", "NaN"),
        ("debt", 100.5),  # a float: amounts are strings
        ("debt", "-1.00"),  # the row forbids a negative debt
        ("overdue_days", True),  # a bool is not an int
        ("overdue_days", "5"),
        ("cutoff_date", "01.09.2026"),
        ("name", 42),
    ],
)
def test_malformed_row_is_rejected_without_echoing_it(field, value):
    data = counterparty_row_to_dict(FULL)
    data[field] = value
    with pytest.raises(PayloadError) as error:
        counterparty_row_from_dict(data)
    assert INN not in str(error.value) and "Ромашка" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", "fatal"),
        ("row", True),
        ("row", "7"),
        ("row", 0),  # the issue requires an Excel row from 1
        ("column", 4),
        ("code", "Not Snake"),
        ("reason", None),
    ],
)
def test_malformed_issue_is_rejected(field, value):
    data = import_issue_to_dict(ISSUE)
    data[field] = value
    with pytest.raises(PayloadError):
        import_issue_from_dict(data)


# --- optional package files: payments, debt history, interactions ---

INN = "1234567894"
PAYMENT = PaymentRow(inn=INN, payment_id="P-1", paid_on=date(2026, 8, 1), amount=Decimal("1500.50"))
SNAPSHOT = DebtSnapshot(inn=INN, cutoff_date=date(2026, 8, 1), debt=Decimal("0"))
TALK = InteractionRow(
    inn=INN,
    interaction_id="INT-1",
    happened_on=date(2026, 8, 20),
    comment="Звонок: обещали оплатить.",
    channel="телефон",
)


@pytest.mark.parametrize(
    "row,dump,load",
    [
        (PAYMENT, payment_row_to_dict, payment_row_from_dict),
        (SNAPSHOT, debt_snapshot_to_dict, debt_snapshot_from_dict),
        (TALK, interaction_row_to_dict, interaction_row_from_dict),
        (
            InteractionRow(
                inn=INN,
                interaction_id="INT-2",
                happened_on=date(2026, 8, 21),
                comment="Без канала.",
            ),
            interaction_row_to_dict,
            interaction_row_from_dict,
        ),
    ],
)
def test_package_rows_round_trip_exactly(row, dump, load):
    data = json.loads(json.dumps(dump(row)))  # through JSON, as the step store keeps it
    assert load(data) == row


@pytest.mark.parametrize(
    "row,dump,load,field,value",
    [
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "amount", 1500.5),  # a float
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "amount", "не число"),
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "amount", "NaN"),
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "amount", "-10"),  # not positive
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "paid_on", "01.08.2026"),
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "payment_id", 1),
        (PAYMENT, payment_row_to_dict, payment_row_from_dict, "inn", "1234567890"),  # checksum
        (SNAPSHOT, debt_snapshot_to_dict, debt_snapshot_from_dict, "debt", "-1"),
        (SNAPSHOT, debt_snapshot_to_dict, debt_snapshot_from_dict, "cutoff_date", None),
        (TALK, interaction_row_to_dict, interaction_row_from_dict, "comment", "   "),
        (TALK, interaction_row_to_dict, interaction_row_from_dict, "comment", True),
        (TALK, interaction_row_to_dict, interaction_row_from_dict, "channel", 5),
    ],
)
def test_malformed_package_row_is_rejected_without_echoing_it(row, dump, load, field, value):
    data = dump(row)
    data[field] = value
    with pytest.raises(PayloadError) as error:
        load(data)
    assert str(value) not in str(error.value)


def test_a_missing_field_of_a_package_row_is_rejected():
    data = payment_row_to_dict(PAYMENT)
    del data["paid_on"]
    with pytest.raises(PayloadError):
        payment_row_from_dict(data)
