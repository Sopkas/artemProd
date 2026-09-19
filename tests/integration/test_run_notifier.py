"""S3-01: the owner gets a summary and the report file when a run is recorded."""

from datetime import UTC, date, datetime

from aiogram.methods import SendDocument, SendMessage

from claims_assistant.application.analysis_pipeline import (
    REPORT_STEP,
    REPORT_VERSION,
    RunSummary,
)
from claims_assistant.application.analysis_queue import RunOutcome
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.scoring import Priority
from claims_assistant.domain.steps import RUN_SCOPE, DeliveryStatus, StepResult, StepStatus
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.notifier import TelegramRunNotifier

OWNER = 42
DAY = date(2026, 9, 19)
SUMMARY = RunSummary(
    companies=3,
    checked=2,
    unchecked=1,
    row_errors=0,
    priorities={Priority.CRITICAL: 1, Priority.UNKNOWN: 1, Priority.LOW: 1},
    budget_exhausted=False,
)


async def finished(repository, files, *, outcome: RunOutcome, with_report: bool = True):
    run = await repository.create_run(OWNER, DAY, DataMode.DEMO)
    await repository.transition(OWNER, run.id, RunStatus.QUEUED)
    await repository.claim_next()
    if with_report:
        path = files.save(run.id, b"xlsx-bytes")
        await repository.save_report(run.id, path)
        await repository.save_step(
            StepResult(
                run_id=run.id,
                inn=RUN_SCOPE,
                step=REPORT_STEP,
                version=REPORT_VERSION,
                status=StepStatus.OK,
                completed_at=datetime.now(UTC),
                payload=SUMMARY.to_payload(),
            )
        )
    return await repository.finish(run.id, outcome)


async def test_summary_then_file_and_delivery_is_recorded(bot, tmp_path):
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    run = await finished(
        repository, files, outcome=RunOutcome(RunStatus.PARTIAL, "Организаций с неполными: 1.")
    )
    await TelegramRunNotifier(bot, repository, files).notify(run)

    message, document = bot.session.calls
    assert isinstance(message, SendMessage) and message.chat_id == OWNER
    assert message.text == texts.run_finished(run, SUMMARY)
    assert "завершена частично" in message.text
    assert "Организаций: 3, проверено полностью: 2" in message.text
    assert "критичный — 1" in message.text and "низкий — 1" in message.text
    assert "недостаточно данных — 1" in message.text
    assert "демонстрационные" in message.text
    assert isinstance(document, SendDocument) and document.chat_id == OWNER
    assert document.document.filename == f"otchet-{DAY.isoformat()}.xlsx"
    artifact = await repository.get_report(OWNER, run.id)
    assert artifact.delivery is DeliveryStatus.DELIVERED


async def test_failed_run_without_report_gets_only_the_summary(bot, tmp_path):
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    run = await finished(
        repository, files, outcome=RunOutcome(RunStatus.FAILED, "Нет файла."), with_report=False
    )
    await TelegramRunNotifier(bot, repository, files).notify(run)
    assert [type(call) for call in bot.session.calls] == [SendMessage]
    assert "Причина: Нет файла." in bot.session.calls[0].text
    assert "/report" not in bot.session.calls[0].text


async def test_send_failure_marks_delivery_failed_and_keeps_the_report(bot, tmp_path):
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    run = await finished(repository, files, outcome=RunOutcome(RunStatus.COMPLETED))

    calls = bot.session.calls
    original = bot.session.make_request

    async def make_request(bot_, method, timeout=None):
        if isinstance(method, SendDocument):
            calls.append(method)
            raise RuntimeError("telegram refused the file")
        return await original(bot_, method, timeout)

    bot.session.make_request = make_request
    try:
        await TelegramRunNotifier(bot, repository, files).notify(run)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the send failure must propagate to the worker's log")
    artifact = await repository.get_report(OWNER, run.id)
    assert artifact.delivery is DeliveryStatus.FAILED and artifact.delivery_error
    assert files.read(artifact.stored_path) == b"xlsx-bytes"
