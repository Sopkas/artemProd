"""Contract tests for StepStore and ReportStore; memory and SQLite must behave alike."""

from datetime import UTC, date, datetime, timedelta

import pytest

from claims_assistant.application.analysis_repository import RunNotFound
from claims_assistant.application.step_store import ReportStore, StepStore
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.steps import RUN_SCOPE, DeliveryStatus, StepResult, StepStatus
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository

OWNER = 42
STRANGER = 99
DAY = date(2026, 9, 19)
START = datetime(2026, 9, 19, 10, tzinfo=UTC)
INN = "1234567894"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path) -> StepStore | ReportStore:
    if request.param == "memory":
        return InMemoryAnalysisRepository(clock=Clock())
    return open_sqlite_repository(tmp_path / "claims.sqlite3", clock=Clock())


async def run_for(store, owner: int = OWNER):
    return await store.create_run(owner, DAY, DataMode.DEMO)


def step(run_id: str, **overrides) -> StepResult:
    fields = dict(
        run_id=run_id,
        inn=INN,
        step="external_fetch",
        version="v1",
        status=StepStatus.OK,
        payload='{"a": 1}',
        completed_at=START,
    )
    fields.update(overrides)
    return StepResult(**fields)


# --- steps ---


async def test_saved_step_is_found_by_its_key(store):
    run = await run_for(store)
    saved = await store.save_step(step(run.id))
    assert saved == step(run.id)
    assert await store.get_step(run.id, INN, "external_fetch", "v1") == saved
    assert await store.get_step(run.id, INN, "external_fetch", "v2") is None
    assert await store.get_step(run.id, "0000000000", "external_fetch", "v1") is None


async def test_saving_the_same_key_again_keeps_the_first_result(store):
    run = await run_for(store)
    first = await store.save_step(step(run.id, payload='{"first": true}'))
    second = await store.save_step(step(run.id, payload='{"second": true}'))
    assert second == first
    assert (await store.get_step(run.id, INN, "external_fetch", "v1")).payload == '{"first": true}'


async def test_run_scoped_and_failed_steps_are_stored(store):
    run = await run_for(store)
    await store.save_step(step(run.id, inn=RUN_SCOPE, step="report", payload=None))
    await store.save_step(
        step(run.id, step="scoring", status=StepStatus.FAILED, payload=None, error="Нет данных")
    )
    listed = await store.list_steps(run.id)
    assert {(item.inn, item.step) for item in listed} == {(RUN_SCOPE, "report"), (INN, "scoring")}
    failed = await store.get_step(run.id, INN, "scoring", "v1")
    assert failed.status is StepStatus.FAILED and failed.error == "Нет данных"


async def test_steps_of_a_missing_run_are_rejected(store):
    with pytest.raises(RunNotFound):
        await store.save_step(step("no-such-run"))
    assert await store.list_steps("no-such-run") == ()


async def test_steps_survive_reopening_sqlite(tmp_path):
    path = tmp_path / "claims.sqlite3"
    first = open_sqlite_repository(path)
    run = await first.create_run(OWNER, DAY, DataMode.DEMO)
    await first.save_step(step(run.id))
    first.close()
    second = open_sqlite_repository(path)
    assert (await second.get_step(run.id, INN, "external_fetch", "v1")) == step(run.id)
    second.close()


# --- report artifacts ---


async def test_report_is_saved_pending_and_visible_to_the_owner_only(store):
    run = await run_for(store)
    report = await store.save_report(run.id, "r/report.xlsx")
    assert report.run_id == run.id
    assert report.delivery is DeliveryStatus.PENDING
    assert report.created_at.tzinfo is UTC
    assert await store.get_report(OWNER, run.id) == report
    with pytest.raises(RunNotFound):
        await store.get_report(STRANGER, run.id)
    assert await store.get_report(OWNER, (await run_for(store)).id) is None


async def test_report_delivery_is_recorded_without_touching_the_run(store):
    run = await run_for(store)
    await store.transition(OWNER, run.id, RunStatus.QUEUED)
    await store.save_report(run.id, "r/report.xlsx")
    delivered = await store.mark_delivery(run.id, DeliveryStatus.DELIVERED)
    assert delivered.delivery is DeliveryStatus.DELIVERED
    assert delivered.delivered_at is not None
    assert (await store.get_run(OWNER, run.id)).status == RunStatus.QUEUED
    failed = await store.mark_delivery(run.id, DeliveryStatus.FAILED, error="Telegram недоступен")
    assert (
        failed.delivery is DeliveryStatus.FAILED and failed.delivery_error == "Telegram недоступен"
    )
    assert failed.delivered_at is None


async def test_regenerated_report_replaces_the_previous_artifact(store):
    run = await run_for(store)
    await store.save_report(run.id, "r/first.xlsx")
    await store.mark_delivery(run.id, DeliveryStatus.DELIVERED)
    again = await store.save_report(run.id, "r/second.xlsx")
    assert again.stored_path == "r/second.xlsx"
    assert again.delivery is DeliveryStatus.PENDING
    assert (await store.get_report(OWNER, run.id)) == again


async def test_report_calls_reject_missing_runs(store):
    with pytest.raises(RunNotFound):
        await store.save_report("no-such-run", "r/x.xlsx")
    with pytest.raises(RunNotFound):
        await store.mark_delivery("no-such-run", DeliveryStatus.DELIVERED)
    run = await run_for(store)
    with pytest.raises(RunNotFound):
        await store.mark_delivery(run.id, DeliveryStatus.DELIVERED)  # no report yet
