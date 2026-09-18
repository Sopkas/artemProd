"""Application scenario: accept the counterparties file, launch the run, read status."""

from datetime import date
from hashlib import sha256

import pytest

from claims_assistant.application.analysis_repository import InvalidTransition, RunNotFound
from claims_assistant.application.check_package import (
    PackageAccepted,
    PackageRejected,
    accept_counterparties,
    latest_run,
    launch_run,
)
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode
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
