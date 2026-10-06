"""Application scenario: accept the counterparties file, launch the run, read status."""

from datetime import date
from hashlib import sha256

import pytest

from claims_assistant.application.analysis_repository import (
    InvalidTransition,
    RunLocked,
    RunNotFound,
)
from claims_assistant.application.check_package import (
    LedgerAccepted,
    PackageAccepted,
    PackageRejected,
    accept_counterparties,
    accept_ledger,
    latest_run,
    launch_run,
)
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

OWNER = 42
DAY = date(2026, 9, 1)  # cut-off date used by the synthetic template


@pytest.fixture
def deps(tmp_path):
    return dict(
        repository=InMemoryAnalysisRepository(),
        files=LocalFileStorage(tmp_path / "uploads"),
        reader=OpenpyxlSheetReader(),
    )


async def test_valid_file_creates_a_draft_with_the_stored_file(deps, tmp_path):
    data = build_counterparties_template()
    result = await accept_counterparties(OWNER, DAY, DataMode.DEMO, data, **deps)
    assert isinstance(result, PackageAccepted)
    assert result.run.status == RunStatus.DRAFT
    assert result.run.owner_id == OWNER
    assert result.run.analysis_date == DAY
    assert len(result.rows) == 2
    assert result.duplicate is False
    stored = result.file
    assert stored.kind == FileKind.COUNTERPARTIES
    assert stored.checksum == sha256(data).hexdigest()
    assert stored.size_bytes == len(data)
    assert stored.stored_path.startswith(f"{result.run.id}/")
    assert (tmp_path / "uploads" / stored.stored_path).read_bytes() == data
    assert (await deps["repository"].get_run(OWNER, result.run.id)).files == (stored,)


async def test_unusable_file_is_rejected_without_creating_a_run(deps, tmp_path):
    result = await accept_counterparties(OWNER, DAY, DataMode.DEMO, b"not a zip", **deps)
    assert isinstance(result, PackageRejected)
    assert result.issues and result.issues[0].code == "workbook_corrupt"
    assert await deps["repository"].list_runs(OWNER) == ()
    assert not list((tmp_path / "uploads").rglob("*")) or not any(
        path.is_file() for path in (tmp_path / "uploads").rglob("*")
    )


async def test_row_errors_are_reported_but_usable_rows_still_make_a_draft(deps):
    from decimal import Decimal

    from claims_assistant.domain.counterparties import CounterpartyRow

    rows = (
        CounterpartyRow(inn="1234567894"),
        CounterpartyRow(inn="1234567894", debt=Decimal("1.00"), cutoff_date=DAY),
        CounterpartyRow(inn="0000000000"),
    )
    data = build_counterparties_template(rows)
    result = await accept_counterparties(OWNER, DAY, DataMode.DEMO, data, **deps)
    assert isinstance(result, PackageAccepted)
    assert [row.inn for row in result.rows] == ["1234567894", "0000000000"]
    assert any(issue.severity == "error" for issue in result.issues)


async def test_same_file_twice_in_one_run_is_not_duplicated(deps, tmp_path):
    data = build_counterparties_template()
    first = await accept_counterparties(OWNER, DAY, DataMode.DEMO, data, **deps)
    second = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, data, run_id=first.run.id, **deps
    )
    assert isinstance(second, PackageAccepted)
    assert second.duplicate is True
    assert second.file == first.file
    assert len((await deps["repository"].get_run(OWNER, first.run.id)).files) == 1
    files = [path for path in (tmp_path / "uploads").rglob("*.xlsx")]
    assert len(files) == 1


async def test_launch_queues_the_draft_and_is_owner_scoped(deps):
    result = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, build_counterparties_template(), **deps
    )
    with pytest.raises(RunNotFound):
        await launch_run(99, result.run.id, deps["repository"])
    queued = await launch_run(OWNER, result.run.id, deps["repository"])
    assert queued.status == RunStatus.QUEUED
    with pytest.raises(InvalidTransition):
        await launch_run(OWNER, result.run.id, deps["repository"])


async def test_latest_run_returns_the_newest_or_none(deps):
    assert await latest_run(OWNER, deps["repository"]) is None
    first = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, build_counterparties_template(), **deps
    )
    second = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, build_counterparties_template(), **deps
    )
    assert first.run.id != second.run.id
    assert (await latest_run(OWNER, deps["repository"])).id == second.run.id
    assert await latest_run(99, deps["repository"]) is None


# --- S4-01: optional «Платежи» and «История долга» files of the draft ---


async def draft(deps):
    from claims_assistant.application.check_package import accept_counterparties

    result = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, build_counterparties_template(), **deps
    )
    return result.run


def payments(rows):
    from claims_assistant.infrastructure.excel.ledgers import build_payments_workbook

    return build_payments_workbook(rows)


def history(rows):
    from claims_assistant.infrastructure.excel.ledgers import build_debt_history_workbook

    return build_debt_history_workbook(rows)


INN_1 = "7707083893"  # first sample row of the counterparties template
PERIOD = Period(date(2026, 6, 1), date(2026, 8, 31))


async def test_payments_are_parsed_against_the_package_and_stored_with_their_period(deps):
    run = await draft(deps)
    data = payments(
        [[INN_1, "P-1", date(2026, 7, 15), 100.0], ["1234567894", "P-2", date(2026, 7, 16), 1.0]]
    )
    result = await accept_ledger(OWNER, run.id, FileKind.PAYMENTS, data, coverage=PERIOD, **deps)
    assert isinstance(result, LedgerAccepted)
    assert result.rows == 1 and not result.duplicate
    assert [issue.code for issue in result.issues] == ["inn_not_in_package"]
    assert result.file.kind is FileKind.PAYMENTS and result.file.coverage == PERIOD
    assert result.file.checksum == sha256(data).hexdigest()
    assert [file.kind for file in result.run.files] == [FileKind.COUNTERPARTIES, FileKind.PAYMENTS]
    assert deps["files"].read(result.file.stored_path) == data


async def test_ledger_without_usable_rows_is_rejected_and_nothing_is_stored(deps, tmp_path):
    run = await draft(deps)
    data = history([["1234567894", date(2026, 8, 1), 100.0]])
    result = await accept_ledger(OWNER, run.id, FileKind.DEBT_HISTORY, data, coverage=None, **deps)
    assert isinstance(result, PackageRejected)
    assert [issue.code for issue in result.issues] == ["inn_not_in_package"]
    assert len((await deps["repository"].get_run(OWNER, run.id)).files) == 1
    stored = list((tmp_path / "uploads").rglob("*"))
    assert len([p for p in stored if p.is_file()]) == 1


async def test_same_ledger_twice_keeps_one_copy(deps, tmp_path):
    run = await draft(deps)
    data = history([[INN_1, date(2026, 8, 1), 100.0]])
    first = await accept_ledger(OWNER, run.id, FileKind.DEBT_HISTORY, data, coverage=None, **deps)
    second = await accept_ledger(OWNER, run.id, FileKind.DEBT_HISTORY, data, coverage=None, **deps)
    assert not first.duplicate and second.duplicate
    assert second.file == first.file
    assert len([p for p in (tmp_path / "uploads").rglob("*") if p.is_file()]) == 2


async def test_payments_require_a_period_and_only_ledger_kinds_are_accepted(deps):
    run = await draft(deps)
    data = payments([[INN_1, "P-1", date(2026, 7, 15), 100.0]])
    with pytest.raises(ValueError):
        await accept_ledger(OWNER, run.id, FileKind.PAYMENTS, data, coverage=None, **deps)
    with pytest.raises(ValueError):
        await accept_ledger(OWNER, run.id, FileKind.COUNTERPARTIES, data, coverage=PERIOD, **deps)


async def test_ledger_is_owner_scoped_and_needs_a_draft(deps):
    run = await draft(deps)
    data = history([[INN_1, date(2026, 8, 1), 100.0]])
    with pytest.raises(RunNotFound):
        await accept_ledger(99, run.id, FileKind.DEBT_HISTORY, data, coverage=None, **deps)
    await launch_run(OWNER, run.id, deps["repository"])
    with pytest.raises(RunLocked):
        await accept_ledger(OWNER, run.id, FileKind.DEBT_HISTORY, data, coverage=None, **deps)


# --- S4-05: the customer's 1C export, stored translated into our sheet ---


class RestampingWriter:
    """Our «Платежи» writer as openpyxl really behaves: every save stamps the time, so the
    same rows never come out as the same bytes twice (30.09.2026, block 5 of the manual
    test: the same export uploaded twice was stored twice)."""

    def __init__(self):
        self._saves = 0

    def build_payments_workbook(self, rows):
        from io import BytesIO
        from zipfile import ZipFile

        from claims_assistant.infrastructure.excel.ledgers import build_payments_workbook

        self._saves += 1
        buffer = BytesIO(build_payments_workbook(rows))
        with ZipFile(buffer, "a") as archive:
            archive.comment = f"saved {self._saves}".encode()
        return buffer.getvalue()


async def test_an_export_is_stored_as_its_companys_and_a_template_as_the_packages(deps):
    from claims_assistant.application.check_package import accept_payments_export
    from claims_assistant.infrastructure.excel import ledgers
    from tests.integration.test_upload_dialog import export_file

    run = await draft(deps)
    exported = await accept_payments_export(
        OWNER, run.id, export_file(), inn=INN_1, sheets=ledgers, **deps
    )
    template = await accept_ledger(
        OWNER,
        run.id,
        FileKind.PAYMENTS,
        payments([[INN_1, "P-1", date(2026, 7, 15), 100.0]]),
        coverage=PERIOD,
        **deps,
    )
    assert (exported.file.inn, template.file.inn) == (INN_1, None)


async def test_the_same_export_twice_keeps_one_copy(deps, tmp_path):
    from claims_assistant.application.check_package import accept_payments_export
    from tests.integration.test_upload_dialog import export_file

    run = await draft(deps)
    data = export_file()
    writer = RestampingWriter()
    first = await accept_payments_export(OWNER, run.id, data, inn=INN_1, sheets=writer, **deps)
    second = await accept_payments_export(OWNER, run.id, data, inn=INN_1, sheets=writer, **deps)
    assert not first.duplicate and second.duplicate
    assert second.file == first.file
    kinds = [file.kind for file in (await deps["repository"].get_run(OWNER, run.id)).files]
    assert kinds == [FileKind.COUNTERPARTIES, FileKind.PAYMENTS]
    assert len([p for p in (tmp_path / "uploads").rglob("*") if p.is_file()]) == 2


async def test_the_same_export_under_another_inn_says_whose_it_already_is(deps):
    """Review B on #76: the repeat was taken as a duplicate and stayed the first company's
    without a word — the user who fixed a wrong INN believed the export was now the other's."""
    from claims_assistant.application.check_package import PackageConflict, accept_payments_export
    from claims_assistant.infrastructure.excel import ledgers
    from tests.integration.test_upload_dialog import export_file

    run = await draft(deps)
    data = export_file()
    first = await accept_payments_export(OWNER, run.id, data, inn=INN_1, sheets=ledgers, **deps)
    again = await accept_payments_export(
        OWNER, run.id, data, inn="7710140679", sheets=ledgers, **deps
    )
    assert isinstance(again, PackageConflict)
    assert INN_1 in again.reason and "Отмена" in again.reason
    files = (await deps["repository"].get_run(OWNER, run.id)).files
    assert [file.inn for file in files if file.kind is FileKind.PAYMENTS] == [INN_1]
    assert first.file in files


async def test_an_export_stored_before_0005_is_a_plain_repeat_not_a_conflict(deps):
    """Review B on #81: a file stored before migration 0005 has no INN, and the conflict
    read «…как выгрузка организации с ИНН .». Whose it is cannot be told, so it stays a
    repeat, as it was before #81."""
    from claims_assistant.application.analysis_repository import NewFile
    from claims_assistant.application.check_package import accept_payments_export
    from claims_assistant.infrastructure.excel import ledgers
    from tests.integration.test_upload_dialog import export_file

    run = await draft(deps)
    data = export_file()
    old = await deps["repository"].add_file(
        OWNER,
        run.id,
        NewFile(FileKind.PAYMENTS, sha256(data).hexdigest(), 10, f"{run.id}/old.xlsx", PERIOD),
    )
    again = await accept_payments_export(OWNER, run.id, data, inn=INN_1, sheets=ledgers, **deps)
    assert isinstance(again, LedgerAccepted) and again.duplicate
    assert again.file == old
