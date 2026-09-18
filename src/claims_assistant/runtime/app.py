import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommandScopeAllPrivateChats

from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.external_guard import GuardedCompanyDataProvider, GuardPolicy
from claims_assistant.application.package_processor import PackageProcessor
from claims_assistant.application.worker import RunWorker
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.cache.memory import TtlSnapshotCache
from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.presentation.telegram.handlers import create_dispatcher
from claims_assistant.presentation.telegram.menu import bot_commands

from .settings import ConfigurationError, Settings

logger = logging.getLogger(__name__)


class SafeFormatter(logging.Formatter):
    def __init__(self, token: str, checko_key: str = "") -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.token = token
        self.checko_key = checko_key

    def formatException(self, exc_info: tuple) -> str:
        return f"error_type={exc_info[0].__name__}"

    def format(self, record: logging.LogRecord) -> str:
        record = logging.makeLogRecord(record.__dict__)
        if record.name == "aiogram" or record.name.startswith("aiogram."):
            # Transport errors may embed a response body with user content.
            record.msg = "telegram_framework_event"
            record.args = ()
        # Avoid reusing exception text formatted by another logging handler.
        record.exc_text = None
        text = super().format(record).replace(self.token, "[REDACTED]")
        return text.replace(self.checko_key, "[REDACTED]") if self.checko_key else text


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(settings.token, settings.checko_api_key))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    # Keep transport failure signals, with their content sanitized by SafeFormatter.
    logging.getLogger("aiogram").setLevel(logging.WARNING)


async def run(settings: Settings) -> None:
    bot = Bot(token=settings.token)
    try:
        await bot.get_me()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ConfigurationError(
                "Для этого бота уже настроен webhook. Отключите его перед локальным запуском."
            )
        await bot.set_my_commands(bot_commands(), scope=BotCommandScopeAllPrivateChats())
        # Migrations run here, before polling: a broken database stops the start, not a user.
        repository = open_sqlite_repository(settings.database_path)
        try:
            logger.info("storage_ready")
            files = LocalFileStorage(settings.storage_path)
            reader = OpenpyxlSheetReader()
            mode = DataMode.LIVE if settings.data_provider == "checko" else DataMode.DEMO
            source = (
                CheckoCompanyDataProvider(settings.checko_api_key)
                if settings.data_provider == "checko"
                else DemoCompanyDataProvider(settings.demo_scenario)
            )
            # Limits, retries and a per-process cache apply to demo and live alike.
            provider = GuardedCompanyDataProvider(
                source,
                GuardPolicy(
                    timeout_seconds=settings.external.timeout_seconds,
                    max_retries=settings.external.max_retries,
                ),
                TtlSnapshotCache(settings.external.cache_ttl_seconds),
                mode=mode,
            )
            dispatcher = create_dispatcher(
                settings.allowed_ids,
                provider,
                repository=repository,
                files=files,
                reader=reader,
                mode=mode,
            )
            # One worker in this process; runs left "running" by a crash go back to the queue.
            worker = RunWorker(repository, PackageProcessor(files, reader))
            await worker.recover()
            worker_task = asyncio.create_task(worker.run_forever(), name="run-worker")
            logger.info("bot_started")
            try:
                await dispatcher.start_polling(
                    bot, allowed_updates=["message"], close_bot_session=False
                )
            finally:
                worker.stop()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
        finally:
            repository.close()
    finally:
        await bot.session.close()
        logger.info("bot_stopped")


def main() -> int:
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN.")
        return 1
    except TelegramNetworkError:
        logger.error("Не удалось подключиться к Telegram. Проверьте сеть и повторите запуск.")
        return 1
    except RepositoryError as exc:
        logger.error("%s Проверьте DATABASE_PATH и права на каталог.", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("bot_failed error_type=%s", type(exc).__name__)
        return 1
    return 0
