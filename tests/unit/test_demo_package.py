"""S6-04: the demo package — four counterparties whose priorities the rules produce.

A demo that shows hand-picked verdicts proves nothing. So the test does what the defence
does: reads the four workbooks through the real import, asks the demo source, runs the
real indicators and the real scoring, and checks the priority each company was promised in
``infrastructure/demo/package.py``. If a rule changes and the story no longer holds, this
fails here rather than on the screen.
"""

from datetime import date

import pytest

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.application.imports import import_counterparties
from claims_assistant.application.ledger_imports import (
    import_debt_history,
    import_interactions,
    import_payments,
)
from claims_assistant.domain.external import DataMode, FactKind, Period, Section
from claims_assistant.domain.indicators import internal_indicators
from claims_assistant.domain.scoring import Priority, assess
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.infrastructure.demo.package import (
    ANALYSIS_DATE,
    BY_KEY,
    COMPANIES,
    PACKAGE_SCENARIOS,
    PAYMENTS_FROM,
    PAYMENTS_TO,
)
from claims_assistant.infrastructure.excel.demo_package import (
    COUNTERPARTIES_FILE,
    FILE_NOTES,
    HISTORY_FILE,
    INTERACTIONS_FILE,
    PAYMENTS_FILE,
    build_demo_package,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader

PERIOD = Period(PAYMENTS_FROM, PAYMENTS_TO)


@pytest.fixture(scope="module")
def package() -> dict[str, bytes]:
    return build_demo_package()


@pytest.fixture(scope="module")
def imported(package):
    """The package as the pipeline sees it: parsed from the files, not from the source."""
    reader = OpenpyxlSheetReader()
    rows = import_counterparties(reader, package[COUNTERPARTIES_FILE], analysis_date=ANALYSIS_DATE)
    assert rows.issues == (), rows.issues
    inns = {row.inn for row in rows.rows}
    ledgers = {
        "payments": import_payments(
            reader, package[PAYMENTS_FILE], known_inns=inns, analysis_date=ANALYSIS_DATE
        ),
        "history": import_debt_history(
            reader, package[HISTORY_FILE], known_inns=inns, analysis_date=ANALYSIS_DATE
        ),
        "interactions": import_interactions(
            reader, package[INTERACTIONS_FILE], known_inns=inns, analysis_date=ANALYSIS_DATE
        ),
    }
    for name, result in ledgers.items():
        assert result.issues == (), (name, result.issues)
    return rows, ledgers


async def _assessment(company, rows, ledgers):
    provider = DemoCompanyDataProvider(by_inn=PACKAGE_SCENARIOS)
    row = next(item for item in rows.rows if item.inn == company.row.inn)
    snapshots = await provider.fetch(CompanyDataRequest(inn=row.inn, sections=tuple(Section)))
    finances = next(item for item in snapshots if item.section is Section.FINANCES)
    indicators = internal_indicators(
        row,
        ANALYSIS_DATE,
        payments=[p for p in ledgers["payments"].rows if p.inn == row.inn],
        periods=(PERIOD,),
        history=[h for h in ledgers["history"].rows if h.inn == row.inn],
        finances=finances,
    )
    return assess(row.inn, list(snapshots), row, indicators=indicators, analysis_date=ANALYSIS_DATE)


@pytest.mark.parametrize("company", COMPANIES, ids=[item.key for item in COMPANIES])
async def test_the_rules_produce_the_priority_the_story_promises(company, imported):
    rows, ledgers = imported
    assessment = await _assessment(company, rows, ledgers)
    assert assessment.priority == Priority(company.expected_priority)


async def test_the_optional_files_are_what_turns_the_quiet_one_into_a_high(imported):
    """«Северный путь» is the argument for the optional files; it has to keep being one."""
    rows, ledgers = imported
    company = BY_KEY["worsening"]
    row = next(item for item in rows.rows if item.inn == company.row.inn)
    assert row.overdue_days is not None and row.overdue_days < 30  # below every threshold
    alone = assess(row.inn, [], row, analysis_date=ANALYSIS_DATE)
    assert alone.priority is not Priority.HIGH
    with_files = await _assessment(company, rows, ledgers)
    assert with_files.priority is Priority.HIGH
    assert {"no_payments_60", "debt_doubled"} <= {s.code for s in with_files.signals}


async def test_the_incomplete_one_says_what_is_missing_instead_of_guessing(imported):
    rows, ledgers = imported
    assessment = await _assessment(BY_KEY["incomplete"], rows, ledgers)
    assert assessment.priority is Priority.UNKNOWN
    assert assessment.missing_data  # named gaps, not a silent «low»


async def test_the_alarm_is_raised_by_a_message_that_still_needs_reading(imported):
    rows, ledgers = imported
    assessment = await _assessment(BY_KEY["alarm"], rows, ledgers)
    codes = {signal.code for signal in assessment.signals}
    assert "unresolved_bankruptcy_event" in codes
    # The register answers by INN whatever the role, so the message is never a verdict.
    assert assessment.priority is Priority.HIGH
    assert assessment.priority is not Priority.CRITICAL


async def test_the_demo_source_tells_a_different_story_per_inn():
    provider = DemoCompanyDataProvider(by_inn=PACKAGE_SCENARIOS)
    events = {}
    for company in COMPANIES:
        snapshots = await provider.fetch(
            CompanyDataRequest(inn=company.row.inn, sections=(Section.BANKRUPTCY,))
        )
        events[company.key] = [
            fact for fact in snapshots[0].facts if fact.kind is FactKind.BANKRUPTCY_EVENT
        ]
        assert all(item.mode is DataMode.DEMO for item in snapshots)
    assert events["alarm"] and not events["ordinary"] and not events["worsening"]


async def test_without_a_mapping_the_provider_behaves_exactly_as_before():
    """Every existing caller passes one scenario; that must keep meaning «for everyone»."""
    provider = DemoCompanyDataProvider(DemoScenario.ALARM)
    for inn in (COMPANIES[0].row.inn, COMPANIES[3].row.inn, "7707083893"):
        assert provider.scenario_for(inn) is DemoScenario.ALARM
    mapped = DemoCompanyDataProvider(DemoScenario.ORDINARY, by_inn=PACKAGE_SCENARIOS)
    assert mapped.scenario_for("7707083893") is DemoScenario.ORDINARY  # unknown INN
    assert mapped.scenario_for(COMPANIES[1].row.inn) is DemoScenario.ALARM
    with pytest.raises(ValueError):
        DemoCompanyDataProvider(by_inn={"7707083893": "alarm"})


def test_the_package_is_the_same_bytes_every_time(package):
    """A rehearsal and the defence must show the same numbers."""
    again = build_demo_package()
    assert set(again) == set(package) == set(FILE_NOTES)
    for name in package:
        assert len(again[name]) == len(package[name])


def test_nothing_in_the_package_pretends_to_be_real(imported):
    rows, _ = imported
    for row in rows.rows:
        assert row.name and "демо" in row.name.lower()
        assert row.cutoff_date in (None, ANALYSIS_DATE)
    assert {row.inn for row in rows.rows} == set(PACKAGE_SCENARIOS)


def test_the_script_and_the_document_describe_the_same_four(package):
    from pathlib import Path

    doc = Path(__file__).resolve().parents[2] / "docs" / "demo-script.md"
    text = doc.read_text(encoding="utf-8")
    for company in COMPANIES:
        # The document names the company; «(демо)» marks the data, not the story.
        name = (company.row.name or "").replace(" (демо)", "")
        assert name in text, company.key
        assert company.expected_priority in text.lower(), company.key
    assert str(date(2026, 9, 1).strftime("%d.%m.%Y")) in text
