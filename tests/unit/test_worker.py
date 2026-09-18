"""One background worker: claims queued runs one at a time, records outcomes, never dies."""

import asyncio
import logging
from datetime import date

import pytest

from claims_assistant.application.analysis_queue import RunOutcome
from claims_assistant.application.worker import RunProcessor, RunWorker
from claims_assistant.domain.analysis import AnalysisRun, RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository

OWNER = 42
DAY = date(2026, 9, 18)


class ScriptedProcessor:
    """Returns or raises what the test scripted, and records the order of runs."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.outcomes: dict[str, RunOutcome | Exception] = {}
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def process(self, run: AnalysisRun) -> RunOutcome:
        self.seen.append(run.id)
        self.started.set()
        await self.release.wait()
        outcome = self.outcomes.get(run.id, RunOutcome(RunStatus.COMPLETED))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def queued(repository) -> AnalysisRun:
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    return await repository.transition(OWNER, run.id, RunStatus.QUEUED)


async def run_once(worker: RunWorker) -> bool:
    return await worker.process_one()


async def test_processes_queued_runs_oldest_first_and_records_completion():
    repository = InMemoryAnalysisRepository()
    first, second = await queued(repository), await queued(repository)
    processor = ScriptedProcessor()
    worker = RunWorker(repository, processor)
    assert await run_once(worker) is True
    assert await run_once(worker) is True
    assert await run_once(worker) is False
    assert processor.seen == [first.id, second.id]
    assert (await repository.get_run(OWNER, first.id)).status == RunStatus.COMPLETED


async def test_processor_outcome_is_stored_including_partial_reason():
    repository = InMemoryAnalysisRepository()
    run = await queued(repository)
    processor = ScriptedProcessor()
    processor.outcomes[run.id] = RunOutcome(RunStatus.PARTIAL, "Часть строк не разобрана")
    await run_once(RunWorker(repository, processor))
    stored = await repository.get_run(OWNER, run.id)
    assert stored.status == RunStatus.PARTIAL
    assert stored.failure == "Часть строк не разобрана"


async def test_processor_exception_fails_the_run_safely_and_keeps_the_worker_alive(caplog):
    repository = InMemoryAnalysisRepository()
    broken, healthy = await queued(repository), await queued(repository)
    processor = ScriptedProcessor()
    processor.outcomes[broken.id] = RuntimeError("private INN 1234567894 and key secret")
    worker = RunWorker(repository, processor)
    with caplog.at_level(logging.ERROR):
        await run_once(worker)
        await run_once(worker)
    failed = await repository.get_run(OWNER, broken.id)
    assert failed.status == RunStatus.FAILED
    assert "1234567894" not in (failed.failure or "")
    assert "secret" not in (failed.failure or "")
    assert failed.failure
    assert "run_failed" in caplog.text
    assert "1234567894" not in caplog.text and "secret" not in caplog.text
    assert (await repository.get_run(OWNER, healthy.id)).status == RunStatus.COMPLETED


async def test_worker_loop_does_not_block_the_event_loop_while_processing():
    repository = InMemoryAnalysisRepository()
    run = await queued(repository)
    processor = ScriptedProcessor()
    processor.release.clear()
    worker = RunWorker(repository, processor, poll_interval=0.01)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(processor.started.wait(), timeout=2)
    # The run is being processed, yet other coroutines keep running.
    assert (await repository.get_run(OWNER, run.id)).status == RunStatus.RUNNING
    processor.release.set()
    await asyncio.wait_for(_until_status(repository, run.id, RunStatus.COMPLETED), timeout=2)
    worker.stop()
    await asyncio.wait_for(task, timeout=2)


async def test_relaunching_a_queued_or_running_run_does_not_create_second_work():
    repository = InMemoryAnalysisRepository()
    run = await queued(repository)
    from claims_assistant.application.analysis_repository import InvalidTransition

    with pytest.raises(InvalidTransition):
        await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    processor = ScriptedProcessor()
    worker = RunWorker(repository, processor)
    await run_once(worker)
    assert await run_once(worker) is False
    assert processor.seen == [run.id]


async def test_startup_recovery_requeues_interrupted_runs():
    repository = InMemoryAnalysisRepository()
    run = await queued(repository)
    await repository.claim_next()  # a previous process died here
    processor = ScriptedProcessor()
    worker = RunWorker(repository, processor)
    recovered = await worker.recover()
    assert [item.id for item in recovered] == [run.id]
    await run_once(worker)
    assert (await repository.get_run(OWNER, run.id)).status == RunStatus.COMPLETED
    assert (await repository.get_run(OWNER, run.id)).attempts == 2


async def test_stop_ends_the_loop_promptly_when_idle():
    worker = RunWorker(InMemoryAnalysisRepository(), ScriptedProcessor(), poll_interval=5)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)


def test_processor_protocol_is_satisfied_by_the_scripted_one():
    assert isinstance(ScriptedProcessor(), RunProcessor)


async def _until_status(repository, run_id: str, status: RunStatus) -> None:
    while (await repository.get_run(OWNER, run_id)).status != status:
        await asyncio.sleep(0.01)
