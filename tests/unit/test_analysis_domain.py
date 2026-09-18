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
        (RunStatus.RUNNING, RunStatus.QUEUED, True),  # restart recovery
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


@pytest.mark.parametrize(
    "path", [".", "./", "C:/outside.xlsx", "C:outside.xlsx", "file:stream", "bad\x00name"]
)
def test_storage_path_is_safe_on_windows_and_posix(path):
    from claims_assistant.application.analysis_repository import NewFile

    with pytest.raises(ValueError):
        make_file(stored_path=path)
    with pytest.raises(ValueError):
        NewFile(FileKind.PAYMENTS, CHECKSUM, 1, path)


@pytest.mark.parametrize(
    "fields",
    [
        {"analysis_date": NOW},
        {"mode": "demo"},
        {"status": "draft"},
        {"files": []},
    ],
)
def test_run_rejects_invalid_or_mutable_values(fields):
    with pytest.raises(ValueError):
        make_run(**fields)


@pytest.mark.parametrize("fields", [{"kind": "payments"}, {"coverage": "2025"}])
def test_file_metadata_requires_domain_types(fields):
    from claims_assistant.application.analysis_repository import NewFile

    with pytest.raises(ValueError):
        make_file(**fields)
    values = dict(kind=FileKind.PAYMENTS, checksum=CHECKSUM, size_bytes=1, stored_path="a.xlsx")
    values.update(fields)
    with pytest.raises(ValueError):
        NewFile(**values)


def test_run_defaults_have_no_attempts_and_no_failure():
    run = make_run()
    assert run.attempts == 0 and run.failure is None


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.PARTIAL])
def test_failure_text_is_allowed_for_failed_and_partial_runs(status):
    run = make_run(status=status, failure="Источник недоступен", attempts=2)
    assert run.failure == "Источник недоступен" and run.attempts == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempts": -1},
        {"attempts": 1.0},
        {"failure": ""},
        {"status": RunStatus.RUNNING, "failure": "x"},
        {"status": RunStatus.COMPLETED, "failure": "x"},
    ],
)
def test_run_rejects_bad_attempts_and_misplaced_failure(overrides):
    with pytest.raises(ValueError):
        make_run(**overrides)
