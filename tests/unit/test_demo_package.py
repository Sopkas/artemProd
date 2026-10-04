"""S6-04: the demo package — four counterparties whose priorities the rules produce.

A demo that shows hand-picked verdicts proves nothing. So the test does what the defence
does: reads the four workbooks through the real import, asks the demo source, runs the
real indicators and the real scoring, and checks the priority each company was promised in
``infrastructure/demo/package.py``. If a rule changes and the story no longer holds, this
fails here rather than on the screen.
"""

import io
import zipfile
from datetime import timedelta

import pytest

from claims_assistant.application.check_package import accept_counterparties, accept_ledger
from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.application.imports import import_counterparties
from claims_assistant.application.ledger_imports import (
    import_debt_history,
    import_interactions,
    import_payments,
)
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode, FactKind, Period, Section
from claims_assistant.domain.indicators import internal_indicators
from claims_assistant.domain.inn import validate_inn
from claims_assistant.domain.scoring import Priority, assess
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.infrastructure.demo.package import (
    ANALYSIS_DATE,
    BY_KEY,
    COMPANIES,
    PACKAGE_NAMES,
    PACKAGE_SCENARIOS,
    PAYMENTS_FROM,
    PAYMENTS_TO,
)
from claims_assistant.infrastructure.excel.demo_package import (
    COUNTERPARTIES_FILE,
    FILE_NOTES,
    FROZEN_AT,
    HISTORY_FILE,
    INTERACTIONS_FILE,
    PAYMENTS_FILE,
    build_demo_package,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.excel.report import PRIORITY_LABELS
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from tests.unit.test_analysis_pipeline import OWNER, guard, pipeline, sheet_rows

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


async def _assessment(company, rows, ledgers, *, payments=True, history=True, by_inn=True):
    """``payments``/``history`` — whether that optional file is in the package; ``by_inn``
    — whether DEMO_PACKAGE is on (off: one scenario for every company)."""
    provider = DemoCompanyDataProvider(by_inn=PACKAGE_SCENARIOS if by_inn else None)
    row = next(item for item in rows.rows if item.inn == company.row.inn)
    snapshots = await provider.fetch(CompanyDataRequest(inn=row.inn, sections=tuple(Section)))
    finances = next(item for item in snapshots if item.section is Section.FINANCES)
    indicators = internal_indicators(
        row,
        ANALYSIS_DATE,
        payments=[p for p in ledgers["payments"].rows if p.inn == row.inn] if payments else (),
        periods=(PERIOD,) if payments else (),
        history=[h for h in ledgers["history"].rows if h.inn == row.inn] if history else (),
        finances=finances,
    )
    return assess(row.inn, list(snapshots), row, indicators=indicators, analysis_date=ANALYSIS_DATE)


@pytest.mark.parametrize("company", COMPANIES, ids=[item.key for item in COMPANIES])
async def test_the_rules_produce_the_priority_the_story_promises(company, imported):
    rows, ledgers = imported
    assessment = await _assessment(company, rows, ledgers)
    assert assessment.priority is company.expected_priority


@pytest.mark.parametrize(
    ("payments", "history", "priority", "signals"),
    [
        (False, False, Priority.UNKNOWN, set()),
        (True, False, Priority.MEDIUM, {"no_payments_60"}),
        (False, True, Priority.MEDIUM, {"debt_doubled"}),
        (True, True, Priority.HIGH, {"no_payments_60", "debt_doubled"}),
    ],
    ids=["row-alone", "with-payments", "with-history", "with-both"],
)
async def test_each_optional_file_adds_a_ground_and_the_two_make_a_high(
    imported, payments, history, priority, signals
):
    """«Северный путь» is the argument for the optional files. The story says what every
    combination gives, and the first question from the room is «take one file away»
    (review A on #61): the whole table is held here, not only its last row."""
    rows, ledgers = imported
    company = BY_KEY["worsening"]
    assert company.row.overdue_days is not None and company.row.overdue_days < 30
    assert company.row.last_payment_date is None  # why the row alone is «unknown»
    result = await _assessment(company, rows, ledgers, payments=payments, history=history)
    assert result.priority is priority
    assert {signal.code for signal in result.signals} == signals


async def test_the_incomplete_one_says_what_is_missing_instead_of_guessing(imported):
    rows, ledgers = imported
    assessment = await _assessment(BY_KEY["incomplete"], rows, ledgers)
    assert assessment.priority is Priority.UNKNOWN
    assert assessment.missing_data  # named gaps, not a silent «low»


async def test_the_alarm_is_raised_by_a_message_that_still_needs_reading(imported):
    rows, ledgers = imported
    assessment = await _assessment(BY_KEY["alarm"], rows, ledgers)
    codes = {signal.code for signal in assessment.signals}
    assert codes == {"overdue_30", "unresolved_bankruptcy_event"}
    # The register answers by INN whatever the role, so the message is never a verdict.
    assert assessment.priority is Priority.HIGH
    assert assessment.priority is not Priority.CRITICAL


async def test_without_the_message_the_alarm_is_a_medium(imported):
    """The message is what raises it — the story says so, and «а без сообщения?» must have
    the answer «средний». It is also how a forgotten DEMO_PACKAGE shows on the screen
    (review A on #61): with one scenario for everybody nothing else would change."""
    rows, ledgers = imported
    forgotten = await _assessment(BY_KEY["alarm"], rows, ledgers, by_inn=False)
    assert forgotten.priority is Priority.MEDIUM
    assert {signal.code for signal in forgotten.signals} == {"overdue_30"}
    for key in ("ordinary", "worsening", "incomplete"):
        same = await _assessment(BY_KEY[key], rows, ledgers, by_inn=False)
        assert same.priority is BY_KEY[key].expected_priority


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


async def test_the_card_names_the_company_of_the_package():
    provider = DemoCompanyDataProvider(by_inn=PACKAGE_SCENARIOS, names=PACKAGE_NAMES)
    for company in COMPANIES:
        snapshots = await provider.fetch(
            CompanyDataRequest(inn=company.row.inn, sections=(Section.COMPANY,))
        )
        names = [f.value for f in snapshots[0].facts if f.kind is FactKind.COMPANY_NAME]
        assert names == [company.row.name]
    assert len(set(PACKAGE_NAMES.values())) == len(COMPANIES)  # four names, not one
    other = await DemoCompanyDataProvider(names=PACKAGE_NAMES).fetch(
        CompanyDataRequest(inn="7707083893", sections=(Section.COMPANY,))
    )
    assert [f.value for f in other[0].facts if f.kind is FactKind.COMPANY_NAME] == [
        "ДЕМО — вымышленная компания"
    ]


def test_the_package_is_the_same_bytes_every_time(package):
    """A package is accepted by its sha256: a file rebuilt and sent again into the same
    check must be the file already there (review A on #61). Equal bytes of two builds in
    one second prove nothing — openpyxl stamps the time of saving — so the stamps
    themselves are checked: every one is the package's date, none is the clock."""
    again = build_demo_package()
    assert set(again) == set(package) == set(FILE_NOTES)
    stamp = "{:04d}-{:02d}-{:02d}T00:00:00Z".format(*FROZEN_AT[:3])
    for name, data in package.items():
        assert again[name] == data, name
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert {item.date_time for item in archive.infolist()} == {FROZEN_AT}, name
            core = archive.read("docProps/core.xml").decode("utf-8")
        assert core.count(stamp) == 2, name  # created and modified
        assert "<dcterms:created" in core and "<dcterms:modified" in core


def test_nothing_in_the_package_pretends_to_be_real(imported):
    rows, _ = imported
    for row in rows.rows:
        assert row.name and "демо" in row.name.lower()
        assert row.cutoff_date in (None, ANALYSIS_DATE)
        # Valid by the checksum, so the product takes it, yet no tax office has the code
        # 0000: the INN cannot belong to a real organisation.
        assert validate_inn(row.inn) == row.inn and row.inn.startswith("0000")
    assert {row.inn for row in rows.rows} == set(PACKAGE_SCENARIOS)


async def _shown(tmp_path, package, *, period=PERIOD, by_inn=True):
    """The package the way the defence gives it to the bot: the four files accepted into a
    check, the check run by the real pipeline, the report read back. Returns the outcome,
    the «Приоритеты» rows by INN and what a rebuilt payments file sent again was taken for."""
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    deps = dict(repository=repository, files=files, reader=OpenpyxlSheetReader())
    draft = await accept_counterparties(
        OWNER, ANALYSIS_DATE, DataMode.DEMO, package[COUNTERPARTIES_FILE], **deps
    )
    run = draft.run
    for kind, name, coverage in (
        (FileKind.PAYMENTS, PAYMENTS_FILE, period),
        (FileKind.DEBT_HISTORY, HISTORY_FILE, None),
        (FileKind.INTERACTIONS, INTERACTIONS_FILE, None),
    ):
        added = await accept_ledger(OWNER, run.id, kind, package[name], coverage=coverage, **deps)
        assert added.issues == () and not added.duplicate, name
    again = await accept_ledger(
        OWNER,
        run.id,
        FileKind.PAYMENTS,
        build_demo_package()[PAYMENTS_FILE],
        coverage=period,
        **deps,
    )
    await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    provider = DemoCompanyDataProvider(
        by_inn=PACKAGE_SCENARIOS if by_inn else None, names=PACKAGE_NAMES if by_inn else None
    )
    outcome = await pipeline(files, repository, guard(provider)).process(run)
    data = files.read((await repository.get_report(OWNER, run.id)).stored_path)
    rows = {row[0]: row for row in sheet_rows(data, "Приоритеты")}
    return outcome, rows, again, len(run.files)


async def test_the_bot_shows_what_the_script_promises(tmp_path, package):
    """Not the rules alone: the acceptance of the files, the package review, the period of
    the upload and the report are where a showing breaks (review A on #61)."""
    outcome, rows, again, files = await _shown(tmp_path, package)
    for company in COMPANIES:
        row = rows[company.row.inn]
        assert PRIORITY_LABELS[company.expected_priority] in row, company.key
        assert company.row.name in row, company.key
    # Three are checked in full and say so; the incomplete one names what is missing, and
    # because of it the check ends as «завершена частично».
    assert ["полная" in rows[c.row.inn] for c in COMPANIES] == [True, True, True, False]
    assert outcome.status is RunStatus.PARTIAL
    # A rebuilt file sent again is the file already in the check, not a second one.
    assert again.duplicate and files == 4


async def test_a_period_one_day_short_makes_the_quiet_one_a_medium(tmp_path, package):
    """Why the script gives the period to the day: «по 31.08» sounds natural, and with it the
    export no longer reaches the analysis date — «нет поступлений» stops being a fact."""
    short = Period(PAYMENTS_FROM, PAYMENTS_TO - timedelta(days=1))
    _, rows, _, _ = await _shown(tmp_path, package, period=short)
    assert PRIORITY_LABELS[Priority.MEDIUM] in rows[BY_KEY["worsening"].row.inn]


async def test_a_forgotten_demo_package_shows_in_the_report(tmp_path, package):
    _, rows, _, _ = await _shown(tmp_path, package, by_inn=False)
    assert PRIORITY_LABELS[Priority.MEDIUM] in rows[BY_KEY["alarm"].row.inn]


def test_another_analysis_date_rejects_the_rows_with_a_cutoff(package):
    """«Сегодня» on the day of the defence: three rows carry the package's cut-off date and
    are refused, one company is left and there is nothing to show."""
    wrong = import_counterparties(
        OpenpyxlSheetReader(),
        package[COUNTERPARTIES_FILE],
        analysis_date=ANALYSIS_DATE + timedelta(days=25),
    )
    assert [row.inn for row in wrong.rows] == [BY_KEY["incomplete"].row.inn]
    assert len(wrong.issues) == 3


def test_the_script_and_the_document_describe_the_same_four(package):
    from pathlib import Path

    doc = Path(__file__).resolve().parents[2] / "docs" / "demo-script.md"
    text = doc.read_text(encoding="utf-8")
    for company in COMPANIES:
        # The document names the company; «(демо)» marks the data, not the story.
        name = (company.row.name or "").replace(" (демо)", "")
        label = PRIORITY_LABELS[company.expected_priority].lower()
        # The heading of the company's scene carries its INN and the priority in the words
        # of the report, so a word met elsewhere in the text does not pass for it.
        heading = next(
            (line for line in text.splitlines() if line.startswith("### ") and name in line), ""
        )
        assert company.row.inn in heading, company.key
        assert label in heading.lower(), company.key
    # What the presenter must type: with another date or period the package falls apart.
    assert f"**`{ANALYSIS_DATE:%d.%m.%Y}`**" in text
    assert f"**`{PAYMENTS_FROM:%d.%m.%Y}–{PAYMENTS_TO:%d.%m.%Y}`**" in text
