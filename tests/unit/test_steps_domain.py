from datetime import UTC, datetime

import pytest

from claims_assistant.domain.steps import (
    RUN_SCOPE,
    DeliveryStatus,
    ReportArtifact,
    StepResult,
    StepStatus,
)

NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)


def make_step(**overrides) -> StepResult:
    fields = dict(
        run_id="r1",
        inn="1234567894",
        step="external_fetch",
        version="checko-v1",
        status=StepStatus.OK,
        payload='{"facts": []}',
        completed_at=NOW,
    )
    fields.update(overrides)
    return StepResult(**fields)


def test_step_key_and_payload_are_kept():
    step = make_step()
    assert step.key == ("r1", "1234567894", "external_fetch", "checko-v1")
    assert step.payload == '{"facts": []}'
    assert step.error is None
    assert step.completed_at.tzinfo is UTC


def test_run_level_step_uses_the_run_scope_marker():
    step = make_step(inn=RUN_SCOPE, step="report")
    assert step.inn == RUN_SCOPE == ""


def test_failed_step_carries_a_safe_error_and_no_payload():
    step = make_step(status=StepStatus.FAILED, payload=None, error="Источник недоступен")
    assert step.error == "Источник недоступен"


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"step": ""},
        {"step": "External Fetch"},
        {"version": ""},
        {"status": "ok"},
        {"status": StepStatus.OK, "error": "x"},
        {"status": StepStatus.FAILED, "error": None},
        {"status": StepStatus.FAILED, "error": "x", "payload": "{}"},
        {"completed_at": datetime(2026, 9, 19, 10)},
        {"inn": "abc"},
        {"inn": "12345678901"},
    ],
)
def test_invalid_step_results_are_rejected(overrides):
    with pytest.raises(ValueError):
        make_step(**overrides)


def make_report(**overrides) -> ReportArtifact:
    fields = dict(run_id="r1", stored_path="r1/report.xlsx", created_at=NOW)
    fields.update(overrides)
    return ReportArtifact(**fields)


def test_report_starts_pending_and_records_delivery_separately():
    report = make_report()
    assert report.delivery is DeliveryStatus.PENDING
    assert report.delivered_at is None and report.delivery_error is None
    delivered = make_report(delivery=DeliveryStatus.DELIVERED, delivered_at=NOW)
    assert delivered.delivered_at == NOW


@pytest.mark.parametrize(
    "overrides",
    [
        {"stored_path": ""},
        {"stored_path": "../x.xlsx"},
        {"created_at": datetime(2026, 9, 19, 10)},
        {"delivery": DeliveryStatus.DELIVERED},  # needs delivered_at
        {"delivery": DeliveryStatus.PENDING, "delivered_at": NOW},
        {"delivery": DeliveryStatus.FAILED},  # needs an error
        {"delivery": DeliveryStatus.DELIVERED, "delivered_at": NOW, "delivery_error": "x"},
    ],
)
def test_invalid_report_artifacts_are_rejected(overrides):
    with pytest.raises(ValueError):
        make_report(**overrides)
