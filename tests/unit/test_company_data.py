from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from claims_assistant.application.company_data import (
    CompanyDataProvider,
    CompanyDataRequest,
    RequestLimits,
)
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    Fact,
    FetchStatus,
    Period,
    Section,
)
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario

# Synthetic identifier, not a lookup of a real organization's risk.
INN = "0000000000"


@pytest.mark.parametrize("scenario", list(DemoScenario))
async def test_scenarios_are_deterministic_offline_and_have_linked_evidence(scenario, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Demo provider must not use a network")

    monkeypatch.setattr("socket.socket.connect", no_network)
    provider: CompanyDataProvider = DemoCompanyDataProvider(scenario)
    request = CompanyDataRequest(INN, limits=RequestLimits(max_requests=1, max_pages=1))
    snapshots = await provider.fetch(request)
    assert snapshots == await provider.fetch(request)
    assert [s.section for s in snapshots] == list(Section)
    for snapshot in snapshots:
        assert snapshot.inn == INN
        assert snapshot.mode == DataMode.DEMO
        assert snapshot.source == "synthetic-demo-v1"
        assert snapshot.fetched_at.tzinfo is UTC
        assert snapshot.source_updated_at is None
        assert all(e.url is None for e in snapshot.evidence)
        for fact in snapshot.facts:
            assert fact.inn == INN
            assert set(fact.evidence_ids) <= {e.id for e in snapshot.evidence}


async def test_empty_complete_selection_differs_from_missing_data_and_error():
    results = {}
    for scenario in DemoScenario:
        results[scenario] = await DemoCompanyDataProvider(scenario).fetch(CompanyDataRequest(INN))
    ordinary = results[DemoScenario.ORDINARY][1]
    alarm = results[DemoScenario.ALARM][1]
    partial = results[DemoScenario.INCOMPLETE][1]
    failed = results[DemoScenario.ERROR][1]
    assert ordinary.facts == () and ordinary.coverage == Coverage.COMPLETE
    assert ordinary.status == FetchStatus.OK and ordinary.covered_period is not None
    assert alarm.facts[0].kind == "bankruptcy_event"
    assert "ДЕМО" in alarm.facts[0].value
    assert partial.facts == () and partial.coverage == Coverage.PARTIAL and partial.missing
    assert partial.status == FetchStatus.OK
    assert failed.facts == () and failed.coverage == Coverage.PARTIAL and failed.error
    assert failed.status == FetchStatus.UNAVAILABLE
    incomplete = results[DemoScenario.INCOMPLETE]
    assert incomplete[0].status == FetchStatus.OK
    assert incomplete[2].status == FetchStatus.UNAVAILABLE


async def test_requested_subset_order_and_exact_zero_are_preserved():
    request = CompanyDataRequest(INN, (Section.FINANCES, Section.COMPANY))
    finance, company = await DemoCompanyDataProvider().fetch(request)
    assert finance.section == Section.FINANCES and company.section == Section.COMPANY
    assert finance.facts[0].value == Decimal("12000000.00")
    assert finance.facts[1].value == Decimal("0.00")
    assert finance.facts[1].missing_reason is None
    assert all(f.unit == "RUB" and isinstance(f.value, Decimal) for f in finance.facts)


@pytest.mark.parametrize("value", [0.1, Decimal("NaN"), Decimal("Infinity")])
def test_inexact_or_nonfinite_fact_is_rejected(value):
    with pytest.raises((TypeError, ValueError)):
        Fact("f", INN, "revenue", value, ("e",), observed_on=date(2025, 12, 31))


def test_unknown_value_requires_reason_and_is_distinct_from_zero():
    with pytest.raises(ValueError, match="reason"):
        Fact("f", INN, "revenue", None, ("e",), observed_on=date(2025, 12, 31))
    fact = Fact(
        "f",
        INN,
        "revenue",
        None,
        ("e",),
        observed_on=date(2025, 12, 31),
        missing_reason="No report",
    )
    assert fact.value is None


async def test_snapshot_rejects_broken_provenance_and_misleading_coverage():
    company = (await DemoCompanyDataProvider().fetch(CompanyDataRequest(INN)))[0]
    invalid = [
        {"facts": (replace(company.facts[0], inn="another"),)},
        {"facts": (replace(company.facts[0], evidence_ids=("missing",)),)},
        {"evidence": company.evidence * 2},
        {"facts": company.facts * 2},
        {"coverage": Coverage.PARTIAL},
        {"missing": ("No data",)},
        {"status": FetchStatus.UNAVAILABLE},
        {"fetched_at": datetime(2026, 1, 1)},
        {"evidence": (replace(company.evidence[0], source="another"),)},
    ]
    for change in invalid:
        with pytest.raises(ValueError):
            replace(company, **change)


def test_timestamps_normalize_to_utc_and_period_cannot_be_reversed():
    snapshot = ExternalSnapshot(
        INN,
        Section.COMPANY,
        "test",
        DataMode.DEMO,
        datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
        FetchStatus.OK,
        Coverage.COMPLETE,
    )
    assert snapshot.fetched_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert snapshot.fetched_at.tzinfo is UTC
    with pytest.raises(ValueError):
        Period(date(2026, 1, 1), date(2025, 1, 1))


@pytest.mark.parametrize("sections", [(), (Section.COMPANY, Section.COMPANY), ("invalid",)])
def test_bad_section_requests_are_rejected(sections):
    with pytest.raises(ValueError):
        CompanyDataRequest(INN, sections)


@pytest.mark.parametrize(
    "limits",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"max_pages": 0},
        {"max_requests": 1.5},
    ],
)
def test_invalid_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        RequestLimits(**limits)
