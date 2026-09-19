"""Tell the owner that a check has finished (S3-01): a summary and the report file.

Runs from the worker task, outside any handler, so it sends by chat id: in a private chat
the chat id is the owner's Telegram id. The summary never contains INNs or debt values.
Delivery is recorded through report_delivery, so a failed send leaves the report
available for /report.
"""

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

from claims_assistant.application.analysis_pipeline import load_summary
from claims_assistant.application.analysis_repository import AnalysisRepository, RepositoryError
from claims_assistant.application.check_package import FileStorage
from claims_assistant.application.report_delivery import (
    ReportUnavailable,
    confirm_delivery,
    fetch_report,
)
from claims_assistant.domain.analysis import AnalysisRun

from . import texts
from .menu import main_menu

logger = logging.getLogger(__name__)


class TelegramRunNotifier:
    def __init__(self, bot: Bot, repository: AnalysisRepository, files: FileStorage) -> None:
        self._bot = bot
        self._repository = repository
        self._files = files

    async def notify(self, run: AnalysisRun) -> None:
        summary = await load_summary(run.id, self._repository)
        await self._bot.send_message(
            run.owner_id, texts.run_finished(run, summary), reply_markup=main_menu()
        )
        try:
            report = await fetch_report(run.owner_id, self._repository, self._files)
        except ReportUnavailable:
            await self._bot.send_message(run.owner_id, texts.REPORT_UNAVAILABLE)
            return
        if report is None or report.run.id != run.id:
            return
        document = BufferedInputFile(report.data, filename=report.filename)
        try:
            await self._bot.send_document(
                run.owner_id,
                document,
                caption=texts.report_caption(run),
                reply_markup=main_menu(),
            )
        except Exception as exc:
            logger.error(
                "report_delivery_failed run_id=%s error_type=%s", run.id, type(exc).__name__
            )
            await confirm_delivery(report, self._repository, error="Не удалось отправить файл.")
            raise
        try:
            await confirm_delivery(report, self._repository)
        except RepositoryError as exc:
            # The owner already has the file; a bookkeeping failure only gets logged.
            logger.error(
                "report_delivery_unrecorded run_id=%s error_type=%s", run.id, type(exc).__name__
            )
