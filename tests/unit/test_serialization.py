"""Domain results as JSON step payloads: exact round trips and strict reading."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.scoring import Priority, assess
from claims_assistant.domain.serialization import (
    SCHEMA,
    PayloadError,
    assessment_from_dict,
    assessment_to_dict,
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
