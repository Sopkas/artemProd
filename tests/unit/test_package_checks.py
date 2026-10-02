"""S4-03: the package as a whole — ownership, one main file, repeats and dates across files."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from claims_assistant.application.check_package import (
    MAIN_FILE_ALREADY_IN_PACKAGE,
    PackageConflict,
    accept_counterparties,
    accept_ledger,
)
from claims_assistant.application.package_checks import (
    DEBT_HISTORY_CONFLICT,
    FOREIGN_FILE,
    LAST_PAYMENT_CONFLICT,
    PACKAGE_SHEET,
    PackageIntegrityError,
    belongs_to_run,
    review_package,
)
from claims_assistant.domain.analysis import FileKind
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.domain.imports import IssueSeverity
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import (
    build_debt_history_workbook,
    build_interactions_workbook,
    build_payments_workbook,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

OWNER = 42
DAY = date(2026, 9, 1)
INN_A, INN_B = "7707083893", "7710140679"
ROWS = (
    CounterpartyRow(
        inn=INN_A, cutoff_date=DAY, debt=Decimal("100.00"), last_payment_date=date(2026, 7, 15)
    ),
    CounterpartyRow(inn=INN_B, cutoff_date=DAY, debt=Decimal("50.00")),
)
PERIOD = Period(date(2026, 6, 1), date(2026, 8, 31))


@pytest.fixture
def deps(tmp_path):
    return dict(
        repository=InMemoryAnalysisRepository(),
        files=LocalFileStorage(tmp_path / "uploads"),
        reader=OpenpyxlSheetReader(),
    )


async def draft(deps, rows=ROWS, data: bytes | None = None):
    result = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, data or build_counterparties_template(rows), **deps
    )
    return result.run


async def attach(deps, run, kind, data, coverage=None):
    result = await accept_ledger(OWNER, run.id, kind, data, coverage=coverage, **deps)
    return result.run


def codes(review):
    return [(i.code, i.severity, i.sheet, i.file_id is not None) for i in review.issues]


async def test_clean_package_reviews_without_findings(deps):
    run = await draft(deps)
    run = await attach(
        deps,
        run,
        FileKind.PAYMENTS,
        build_payments_workbook([[INN_A, "P-1", date(2026, 7, 15), 100.0]]),
        PERIOD,
    )
    run = await attach(
        deps,
        run,
        FileKind.DEBT_HISTORY,
        build_debt_history_workbook([[INN_A, DAY, 100.0], [INN_A, date(2026, 8, 1), 80.0]]),
    )
    run = await attach(
        deps,
        run,
        FileKind.INTERACTIONS,
        build_interactions_workbook([[INN_A, "I-1", date(2026, 8, 20), "Звонок.", None]]),
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert review.issues == () and review.blocking == ()
    assert [r.inn for r in review.counterparties] == [INN_A, INN_B]
    assert len(review.payments) == 1 and len(review.history) == 2
    assert len(review.interactions) == 1


async def test_file_of_another_run_is_never_read(deps, tmp_path):
    run = await draft(deps)
    other = await draft(deps)
    # A record pointing at another run's directory: the storage layer would happily read
    # it, so the review refuses before touching the disk.
    foreign = replace(run.files[0], stored_path=other.files[0].stored_path)
    tampered = replace(run, files=(foreign,))
    with pytest.raises(PackageIntegrityError, match=FOREIGN_FILE):
        await review_package(tampered, deps["files"], deps["reader"])


async def test_second_different_counterparties_file_is_a_conflict(deps):
    # One set of bytes: openpyxl stamps the creation time into the workbook, so two
    # builds a second apart would differ by checksum and look like different files.
    same = build_counterparties_template(ROWS)
    run = await draft(deps, data=same)
    other = build_counterparties_template((CounterpartyRow(inn=INN_A, cutoff_date=DAY),))
    result = await accept_counterparties(OWNER, DAY, DataMode.DEMO, other, run_id=run.id, **deps)
    assert result == PackageConflict(MAIN_FILE_ALREADY_IN_PACKAGE)
    assert len((await deps["repository"].get_run(OWNER, run.id)).files) == 1
    # The same file again stays a duplicate, not a conflict.
    again = await accept_counterparties(OWNER, DAY, DataMode.DEMO, same, run_id=run.id, **deps)
    assert again.duplicate is True


async def test_two_main_files_block_the_launch(deps):
    run = await draft(deps)
    # Bypass the dialog: a second main file recorded directly (older data, a bug, a race).
    from claims_assistant.application.analysis_repository import NewFile

    path = deps["files"].save(run.id, b"other")
    await deps["repository"].add_file(
        OWNER,
        run.id,
        NewFile(kind=FileKind.COUNTERPARTIES, checksum="b" * 64, size_bytes=5, stored_path=path),
    )
    run = await deps["repository"].get_run(OWNER, run.id)
    review = await review_package(run, deps["files"], deps["reader"])
    assert [i.code for i in review.blocking] == ["counterparties_duplicated"]


async def test_ids_repeated_across_files_warn_or_error(deps):
    run = await draft(deps)
    same = [INN_A, "P-1", date(2026, 7, 15), 100.0]
    run = await attach(deps, run, FileKind.PAYMENTS, build_payments_workbook([same]), PERIOD)
    run = await attach(
        deps,
        run,
        FileKind.PAYMENTS,
        build_payments_workbook([same, [INN_A, "P-2", date(2026, 7, 20), 5.0]]),
        PERIOD,
    )
    run = await attach(
        deps,
        run,
        FileKind.PAYMENTS,
        build_payments_workbook([[INN_A, "P-2", date(2026, 7, 21), 5.0]]),
        PERIOD,
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert [(c, s) for c, s, *_ in codes(review)] == [
        ("duplicate_payment_across_files", IssueSeverity.WARNING),
        ("payment_id_conflict_across_files", IssueSeverity.ERROR),
        ("last_payment_conflict", IssueSeverity.WARNING),
    ]
    assert all(file_id for _, _, _, file_id in codes(review)[:2])
    assert [p.payment_id for p in review.payments] == ["P-1", "P-2"]
    assert review.blocking == ()  # a file-level error is explained, not a launch stopper


async def test_snapshot_conflict_across_history_files(deps):
    run = await draft(deps)
    run = await attach(
        deps, run, FileKind.DEBT_HISTORY, build_debt_history_workbook([[INN_A, DAY, 100.0]])
    )
    run = await attach(
        deps, run, FileKind.DEBT_HISTORY, build_debt_history_workbook([[INN_A, DAY, 90.0]])
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert [c for c, *_ in codes(review)] == ["snapshot_conflict_across_files"]
    assert [s.debt for s in review.history] == [Decimal("100.00")]


async def test_debt_history_on_the_analysis_date_must_match_the_main_file(deps):
    run = await draft(deps)
    run = await attach(
        deps, run, FileKind.DEBT_HISTORY, build_debt_history_workbook([[INN_A, DAY, 120.0]])
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert [(c, s, sheet) for c, s, sheet, _ in codes(review)] == [
        ("debt_history_conflict", IssueSeverity.WARNING, PACKAGE_SHEET)
    ]
    reason = review.issues[0].reason
    assert "120" not in reason and INN_A not in reason
    assert "1 организации" in reason and "неизвестной до исправления" in reason
    assert review.conflicts == {INN_A: frozenset({DEBT_HISTORY_CONFLICT})}


async def test_debt_history_is_compared_on_the_rows_own_cutoff_date(deps):
    """Review B on #31: the main file's debt is as of the row's cut-off, not the analysis day.

    The «Контрагенты» parser (S2-04) already rejects a cut-off other than the analysis
    date, so the rows are built directly: the rule must hold even if that changes.
    """
    from claims_assistant.application.package_checks import date_conflicts
    from claims_assistant.domain.debt_history import DebtSnapshot

    run = await draft(deps)
    cutoff = date(2026, 8, 31)
    rows = (CounterpartyRow(inn=INN_A, cutoff_date=cutoff, debt=Decimal("100.00")),)
    # 31.08 = 150 contradicts the row; 19.09 (analysis date) = 120 is simply a later snapshot.
    history = (
        DebtSnapshot(INN_A, cutoff, Decimal("150.00")),
        DebtSnapshot(INN_A, DAY, Decimal("120.00")),
    )
    issues, conflicts = date_conflicts(run, rows, (), history, {})
    assert conflicts == {INN_A: frozenset({DEBT_HISTORY_CONFLICT})}
    assert [i.code for i in issues] == ["debt_history_conflict"]
    # A matching 31.08 snapshot is clean, whatever the analysis-day snapshot says.
    history = (
        DebtSnapshot(INN_A, cutoff, Decimal("100.00")),
        DebtSnapshot(INN_A, DAY, Decimal("120.00")),
    )
    assert date_conflicts(run, rows, (), history, {}) == ([], {})
    # Without a cut-off on the row the analysis date is the row's date.
    rows = (CounterpartyRow(inn=INN_A, debt=Decimal("100.00")),)
    _, conflicts = date_conflicts(run, rows, (), history, {})
    assert conflicts == {INN_A: frozenset({DEBT_HISTORY_CONFLICT})}


async def test_conflicts_are_collected_for_every_company_and_counted_once(deps):
    rows = (
        CounterpartyRow(
            inn=INN_A, cutoff_date=DAY, debt=Decimal("100.00"), last_payment_date=date(2026, 7, 15)
        ),
        CounterpartyRow(
            inn=INN_B, cutoff_date=DAY, debt=Decimal("50.00"), last_payment_date=date(2026, 7, 16)
        ),
    )
    run = await draft(deps, rows)
    run = await attach(
        deps,
        run,
        FileKind.DEBT_HISTORY,
        build_debt_history_workbook([[INN_A, DAY, 1.0], [INN_B, DAY, 2.0]]),
    )
    run = await attach(
        deps,
        run,
        FileKind.PAYMENTS,
        build_payments_workbook([[INN_A, "P-1", date(2026, 7, 1), 1.0]]),
        PERIOD,
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert review.conflicts == {
        INN_A: frozenset({DEBT_HISTORY_CONFLICT, LAST_PAYMENT_CONFLICT}),
        INN_B: frozenset({DEBT_HISTORY_CONFLICT, LAST_PAYMENT_CONFLICT}),
    }
    assert [c for c, *_ in codes(review)] == ["debt_history_conflict", "last_payment_conflict"]
    assert "У 2 организаций" in review.issues[0].reason
    assert "У 2 организаций" in review.issues[1].reason


@pytest.mark.parametrize(
    "path, ok",
    [
        ("{run}/file.xlsx", True),
        ("{run}/../other/file.xlsx", False),
        ("{run}/sub/file.xlsx", False),
        ("other/file.xlsx", False),
        ("{run}-x/file.xlsx", False),
        ("{run}/..", False),
    ],
)
async def test_belongs_to_run_accepts_only_a_plain_file_in_the_runs_directory(deps, path, ok):
    from claims_assistant.domain.analysis import UploadedFile

    run = await draft(deps)
    stored = path.format(run=run.id)
    try:
        file = replace(run.files[0], stored_path=stored)
    except ValueError:
        # The domain refuses the path outright; that is as good as a failed check.
        assert not ok
        return
    assert isinstance(file, UploadedFile)
    assert belongs_to_run(file, run) is ok


@pytest.mark.parametrize(
    "payments, conflict",
    [
        ([[INN_A, "P-1", date(2026, 7, 15), 100.0]], False),  # matches the main file
        ([[INN_A, "P-1", date(2026, 7, 10), 100.0]], True),  # no payment on the stated date
        ([[INN_A, "P-1", date(2026, 7, 15), 1.0], [INN_A, "P-2", date(2026, 8, 2), 1.0]], True),
        ([[INN_B, "P-1", date(2026, 7, 15), 100.0]], True),  # INN_A has none in the period
    ],
)
async def test_last_payment_date_is_checked_against_the_covered_export(deps, payments, conflict):
    run = await draft(deps)
    run = await attach(deps, run, FileKind.PAYMENTS, build_payments_workbook(payments), PERIOD)
    review = await review_package(run, deps["files"], deps["reader"])
    assert ("last_payment_conflict" in [c for c, *_ in codes(review)]) is conflict


async def export(deps, run, inn, rows):
    from claims_assistant.application.check_package import accept_payments_export
    from claims_assistant.infrastructure.excel import ledgers
    from tests.integration.test_upload_dialog import export_file

    data = export_file(rows)  # period 01.06.2026–31.08.2026 from its header
    result = await accept_payments_export(OWNER, run.id, data, inn=inn, sheets=ledgers, **deps)
    return result.run


PAID_15_07 = ("15.07.2026", "Поступление на расчетный счет 00БП-1 от 15.07.2026", 100.0)
PAID_02_08 = ("02.08.2026", "Поступление на расчетный счет 00БП-2 от 02.08.2026", 1.0)


async def test_a_1c_export_vouches_only_for_its_own_company(deps):
    """Block 5 of the manual test (30.09.2026): one company's export made every other company
    «contradict» it — its period was taken as the whole package's."""
    rows = (
        ROWS[0],  # INN_A, last payment 15.07 — matches its export
        CounterpartyRow(
            inn=INN_B, cutoff_date=DAY, debt=Decimal("50.00"), last_payment_date=date(2026, 8, 1)
        ),
    )
    run = await export(deps, await draft(deps, rows), INN_A, [PAID_15_07])
    review = await review_package(run, deps["files"], deps["reader"])
    assert review.conflicts == {}
    assert "last_payment_conflict" not in [c for c, *_ in codes(review)]


async def test_a_1c_export_still_contradicts_its_own_company(deps):
    run = await export(deps, await draft(deps), INN_A, [PAID_15_07, PAID_02_08])
    review = await review_package(run, deps["files"], deps["reader"])
    assert review.conflicts == {INN_A: frozenset({LAST_PAYMENT_CONFLICT})}


async def test_a_companys_periods_are_the_packages_files_and_its_own_exports(deps):
    """What the card and the report count on for one company: our template's period covers
    every company of the package, a 1C export's only the company it was made for."""
    from claims_assistant.application.package_checks import payment_periods

    run = await draft(deps)
    run = await attach(
        deps, run, FileKind.PAYMENTS, build_payments_workbook([[INN_A, "P-9", DAY, 1.0]]), PERIOD
    )
    run = await export(deps, run, INN_A, [PAID_15_07])
    exported = Period(date(2026, 6, 1), date(2026, 8, 31))
    assert payment_periods(run, INN_A) == (PERIOD, exported)
    assert payment_periods(run, INN_B) == (PERIOD,)


async def test_last_payment_outside_the_covered_period_is_not_a_conflict(deps):
    run = await draft(deps)
    run = await attach(
        deps,
        run,
        FileKind.PAYMENTS,
        build_payments_workbook([[INN_A, "P-1", date(2026, 8, 10), 1.0]]),
        Period(date(2026, 8, 1), date(2026, 8, 31)),  # the main file's 15.07 is outside
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert [c for c, *_ in codes(review)] == ["last_payment_conflict"]  # later payment inside
    run2 = await draft(deps)
    run2 = await attach(
        deps,
        run2,
        FileKind.PAYMENTS,
        build_payments_workbook([[INN_B, "P-1", date(2026, 8, 10), 1.0]]),
        Period(date(2026, 8, 1), date(2026, 8, 31)),
    )
    review = await review_package(run2, deps["files"], deps["reader"])
    assert review.issues == ()


async def test_per_file_issues_are_tagged_with_their_file(deps):
    run = await draft(deps)
    run = await attach(
        deps,
        run,
        FileKind.INTERACTIONS,
        build_interactions_workbook(
            [
                [INN_A, "I-1", date(2026, 8, 20), "Звонок.", None],
                ["1234567894", "I-2", date(2026, 8, 20), "Чужой.", None],
            ]
        ),
    )
    review = await review_package(run, deps["files"], deps["reader"])
    assert [(c, file_id) for c, _, _, file_id in codes(review)] == [("inn_not_in_package", True)]
    assert review.issues[0].file_id == run.files[1].id
