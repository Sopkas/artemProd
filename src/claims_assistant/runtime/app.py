import asyncio
import logging
import sys
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommandScopeAllPrivateChats

from claims_assistant.application.analysis_pipeline import AnalysisPipeline, RunLimits
from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.card_review import CardReviewer
from claims_assistant.application.external_guard import GuardedCompanyDataProvider, GuardPolicy
from claims_assistant.application.monitoring import (
    MonitoringPolicy,
    MonitoringScheduler,
    MonitoringService,
)
from claims_assistant.application.recommendation import AiLimits
from claims_assistant.application.retention import RetentionSweeper
from claims_assistant.application.worker import RunWorker
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.cache.memory import TtlSnapshotCache
from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.demo.package import PACKAGE_NAMES, PACKAGE_SCENARIOS
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.excel.report import build_report
from claims_assistant.infrastructure.persistence.ai_spend import SqliteAiSpendStore
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.persistence.watch import SqliteWatchStore
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.presentation.telegram.handlers import create_dispatcher
from claims_assistant.presentation.telegram.menu import bot_commands
from claims_assistant.presentation.telegram.monitoring_notifier import (
    TelegramMonitoringNotifier,
    TelegramMonitoringTexts,
)
from claims_assistant.presentation.telegram.notifier import TelegramRunNotifier

from .ai import build_recommendation_provider, run_limits
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


def company_source(settings: Settings) -> CheckoCompanyDataProvider | DemoCompanyDataProvider:
    """The external source the settings name: Checko, or the demo with the defence package's
    answers per INN when DEMO_PACKAGE is on (S6-04; the e2e test of the show builds it here)."""
    if settings.data_provider == "checko":
        return CheckoCompanyDataProvider(settings.checko_api_key)
    return DemoCompanyDataProvider(
        settings.demo_scenario,
        by_inn=PACKAGE_SCENARIOS if settings.demo_package else None,
        names=PACKAGE_NAMES if settings.demo_package else None,
    )


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
            source = company_source(settings)
            # A forgotten DEMO_PACKAGE turns four stories into one; the log says which it is.
            logger.info(
                "data_provider=%s demo_package=%s", settings.data_provider, settings.demo_package
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
            # S5-01/S5-02: the recommendation provider, if any; every answer is stored.
            recommendation_provider = build_recommendation_provider(settings)
            logger.info(
                "ai_provider=%s send_comments=%s card_review=%s",
                recommendation_provider.name if recommendation_provider else "off",
                settings.ai_send_comments,
                settings.ai_card_review and recommendation_provider is not None,
            )
            # S8-09: the list, its snapshots and messages live next to the checks.
            engine = getattr(repository, "engine", None)
            watch_store = SqliteWatchStore(engine) if engine is not None else None
            monitoring = _monitoring(settings, watch_store, provider, mode)
            dispatcher = create_dispatcher(
                settings.allowed_ids,
                provider,
                repository=repository,
                files=files,
                reader=reader,
                mode=mode,
                business_utc_offset_hours=settings.business_utc_offset_hours,
                card_reviewer=_card_reviewer(settings, recommendation_provider, repository),
                monitoring=monitoring,
            )
            # One worker in this process; runs left "running" by a crash go back to the queue.
            pipeline = AnalysisPipeline(
                files,
                reader,
                provider,
                repository,
                mode=mode,
                build_report=build_report,
                limits=RunLimits(
                    max_requests=settings.external.run_request_limit,
                    max_seconds=settings.external.run_time_limit_seconds,
                ),
                explainer=recommendation_provider,
                ai_limits=recommendation_provider.policy.limits
                if recommendation_provider
                else AiLimits(),
                ai_run_limits=run_limits(settings),
                ai_send_comments=settings.ai_send_comments,
                # S5-03: the month's spend lives next to the runs, so a restart does not
                # hand the model a fresh budget.
                ai_spend=_spend_store(repository),
                ai_month_limit_rub=settings.ai.month_rub_limit,
            )
            notifier = TelegramRunNotifier(bot, repository, files)
            worker = RunWorker(repository, pipeline, notifier=notifier)
            await worker.recover()
            worker_task = asyncio.create_task(worker.run_forever(), name="run-worker")
            # Retention (S6-02): expired checks and their files go at start and periodically.
            sweeper = RetentionSweeper(
                repository,
                files,
                retention=timedelta(days=settings.retention_days),
                interval=settings.retention_sweep_seconds,
            )
            sweeper_task = asyncio.create_task(sweeper.run_forever(), name="retention-sweeper")
            tasks = [worker_task, sweeper_task]
            scheduler = None
            if monitoring is not None:
                # S8-09: a day missed while the computer was off runs at start.
                watch_notifier = TelegramMonitoringNotifier(bot, watch_store)
                scheduler = MonitoringScheduler(monitoring, watch_notifier.deliver)
                tasks.append(asyncio.create_task(scheduler.run_forever(), name="monitoring"))
            logger.info(
                "bot_started monitoring=%s",
                f"{settings.monitoring.run_at:%H:%M}" if monitoring is not None else "off",
            )
            try:
                await dispatcher.start_polling(
                    bot, allowed_updates=["message", "callback_query"], close_bot_session=False
                )
            finally:
                worker.stop()
                sweeper.stop()
                if scheduler is not None:
                    scheduler.stop()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            repository.close()
    finally:
        await bot.session.close()
        logger.info("bot_stopped")


def _monitoring(
    settings: Settings, store: SqliteWatchStore | None, provider, mode: DataMode
) -> MonitoringService | None:
    """S8-09: the daily run over the same guarded source as the cards and checks, so its
    retries, timeouts and the daily-limit stop are the same ones. A repository without a
    database (tests) has nowhere to keep the list: monitoring is off then, and says so."""
    if not settings.monitoring.enabled or store is None:
        return None
    policy = MonitoringPolicy(
        run_at=settings.monitoring.run_at,
        utc_offset_hours=settings.business_utc_offset_hours,
        quiet_summary=settings.monitoring.quiet_summary,
        max_requests=settings.monitoring.request_limit,
        keep_messages_days=settings.retention_days,
        overdue_days=settings.monitoring.overdue_days,
    )
    return MonitoringService(store, provider, TelegramMonitoringTexts(), mode=mode, policy=policy)


def _card_reviewer(settings: Settings, explainer, repository: object) -> CardReviewer | None:
    """S8-05: the reading under the /inn card — the same model, money and month as the
    checks, so a card and a check draw on one agreed budget."""
    if not settings.ai_card_review or explainer is None:
        return None
    return CardReviewer(
        explainer,
        limits=explainer.policy.limits,
        spend=_spend_store(repository),
        month_limit_rub=settings.ai.month_rub_limit,
    )


def _spend_store(repository: object) -> SqliteAiSpendStore | None:
    """The monthly AI spend on the same database, when there is one (S5-03).

    A repository without a database — in tests, or a future in-memory one — has no place
    to keep a month that outlives the process, and saying so is better than pretending:
    the run's own money limit still holds.
    """
    engine = getattr(repository, "engine", None)
    return SqliteAiSpendStore(engine) if engine is not None else None


def check(settings: Settings) -> int:
    """Offline start check for the service (S6-01): settings are readable, the database
    opens and migrates, the storage directories are writable. No Telegram call, so it
    is safe as ExecStartPre and on a machine without network."""
    try:
        repository = open_sqlite_repository(settings.database_path)
        repository.close()
        for directory in (settings.storage_path, settings.backup_path):
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-check"
            probe.write_bytes(b"")
            probe.unlink()
    except (RepositoryError, OSError) as exc:
        print(f"Ошибка проверки: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        f"config_ok data_provider={settings.data_provider} ai_provider={settings.ai_provider} "
        f"database={settings.database_path} storage={settings.storage_path}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 2
    if args == ["--check"]:
        return check(settings)
    if args:
        print("Использование: python -m claims_assistant [--check]", file=sys.stderr)
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
