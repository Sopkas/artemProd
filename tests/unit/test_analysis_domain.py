from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from claims_assistant.domain.analysis import (
    AnalysisRun,
    FileKind,
    RunStatus,
    UploadedFile,
    can_transition,
)
from claims_assistant.domain.external import DataMode, Period

CHECKSUM = "a" * 64
NOW = datetime(2026, 9, 17, 10, tzinfo=UTC)


def make_file(**overrides) -> UploadedFile:
    fields = dict(
        id="f1",
        run_id="r1",
        kind=FileKind.COUNTERPARTIES,
        checksum=CHECKSUM,
        size_bytes=1024,
        stored_path="r1/counterparties.xlsx",
        uploaded_at=NOW,
    )
    fields.update(overrides)
    return UploadedFile(**fields)


def make_run(**overrides) -> AnalysisRun:
    fields = dict(
        id="r1",
        owner_id=42,
        analysis_date=date(2026, 9, 17),
        mode=DataMode.DEMO,
        status=RunStatus.DRAFT,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return AnalysisRun(**fields)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (RunStatus.DRAFT, RunStatus.QUEUED, True),
        (RunStatus.QUEUED, RunStatus.RUNNING, True),
        (RunStatus.RUNNING, RunStatus.COMPLETED, True),
        (RunStatus.RUNNING, RunStatus.PARTIAL, True),
        (RunStatus.RUNNING, RunStatus.FAILED, True),
        (RunStatus.DRAFT, RunStatus.RUNNING, False),
        (RunStatus.DRAFT, RunStatus.COMPLETED, False),
        (RunStatus.QUEUED, RunStatus.DRAFT, False),
        (RunStatus.COMPLETED, RunStatus.RUNNING, False),
        (RunStatus.FAILED, RunStatus.QUEUED, False),
        (RunStatus.RUNNING, RunStatus.RUNNING, False),
    ],
)
def test_status_transitions_follow_the_architecture(current, target, allowed):
    assert can_transition(current, target) is allowed


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED])
def test_final_statuses_have_no_exits(status):
    assert all(not can_transition(status, target) for target in RunStatus)


def test_run_normalizes_technical_timestamps_to_utc():
    local = datetime(2026, 9, 17, 18, tzinfo=timezone(timedelta(hours=8)))
    run = make_run(created_at=local, updated_at=local)
    assert run.created_at == datetime(2026, 9, 17, 10, tzinfo=UTC)
    assert run.created_at.tzinfo is UTC


@pytest.mark.parametrize(
    "overrides",
    [
        {"created_at": datetime(2026, 9, 17, 10)},
        {"owner_id": 0},
        {"owner_id": -1},
        {"id": ""},
        {"updated_at": NOW - timedelta(seconds=1)},
    ],
)
def test_run_rejects_naive_time_bad_owner_empty_id_and_time_going_backwards(overrides):
    with pytest.raises(ValueError):
        make_run(**overrides)


def test_run_files_must_belong_to_the_run():
    with pytest.raises(ValueError):
        make_run(files=(make_file(run_id="other"),))


@pytest.mark.parametrize(
    "overrides",
    [
        {"checksum": "A" * 64},
        {"checksum": "a" * 63},
        {"checksum": "z" * 64},
        {"size_bytes": 0},
        {"stored_path": ""},
        {"stored_path": "/abs/path.xlsx"},
        {"stored_path": "../escape.xlsx"},
        {"uploaded_at": datetime(2026, 9, 17, 10)},
    ],
)
def test_file_rejects_bad_checksum_size_path_and_naive_time(overrides):
    with pytest.raises(ValueError):
        make_file(**overrides)


def test_file_coverage_period_is_optional_and_kept():
    period = Period(date(2026, 1, 1), date(2026, 6, 30))
    assert make_file(kind=FileKind.PAYMENTS, coverage=period).coverage == period
    assert make_file().coverage is None


def test_file_kinds_match_the_data_contracts():
    assert {kind.value for kind in FileKind} == {
        "counterparties",
        "payments",
        "interactions",
        "debt_history",
    }
