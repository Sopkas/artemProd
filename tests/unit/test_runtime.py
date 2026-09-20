import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.external_guard import GuardedCompanyDataProvider
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.runtime import app
from claims_assistant.runtime.settings import ConfigurationError, Settings

TOKEN = "123456789:synthetic_token_for_offline_tests_only"


def test_safe_formatter_redacts_token_and_exception_content():
    try:
        raise RuntimeError("private customer comment")
    except RuntimeError:
        record = logging.LogRecord("test", logging.ERROR, "", 0, "URL/%s", (TOKEN,), sys.exc_info())
    output = app.SafeFormatter(TOKEN).format(record)
    assert TOKEN not in output
    assert "private customer comment" not in output
    assert "RuntimeError" in output


def test_framework_error_body_is_not_logged():
    record = logging.LogRecord(
        "aiogram.dispatcher", logging.ERROR, "", 0, "response: %s", ("private text",), None
    )
    output = app.SafeFormatter(TOKEN).format(record)
    assert "telegram_framework_event" in output
    assert "private text" not in output
    assert record.msg == "response: %s"


@pytest.mark.parametrize("failure", [None, "polling", "startup", "webhook"])
async def test_startup_commands_and_session_cleanup(monkeypatch, failure):
    bot = SimpleNamespace(
        get_me=AsyncMock(),
        get_webhook_info=AsyncMock(
            return_value=SimpleNamespace(url="https://example.org" if failure == "webhook" else "")
        ),
        set_my_commands=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    dispatcher = SimpleNamespace(start_polling=AsyncMock())
    if failure == "polling":
        dispatcher.start_polling.side_effect = RuntimeError("test failure")
    if failure == "startup":
        bot.set_my_commands.side_effect = RuntimeError("test failure")
    monkeypatch.setattr(app, "Bot", Mock(return_value=bot))
    create_dispatcher = Mock(return_value=dispatcher)
    monkeypatch.setattr(app, "create_dispatcher", create_dispatcher)
    repository = SimpleNamespace(close=Mock(), recover_interrupted=AsyncMock(return_value=()))
    open_repository = Mock(return_value=repository)
    monkeypatch.setattr(app, "open_sqlite_repository", open_repository)
    settings = Settings(
        TOKEN,
        frozenset({42}),
        demo_scenario=DemoScenario.ALARM,
        database_path=Path("x/claims.sqlite3"),
    )
    if failure:
        with pytest.raises(ConfigurationError if failure == "webhook" else RuntimeError):
            await app.run(settings)
    else:
        await app.run(settings)
        provider = create_dispatcher.call_args.args[1]
        # The dispatcher gets the guarded provider (limits, retries, cache) around the demo one.
        assert isinstance(provider, GuardedCompanyDataProvider)
        assert isinstance(provider.inner, DemoCompanyDataProvider)
        assert provider.inner.scenario == DemoScenario.ALARM
        repository.recover_interrupted.assert_awaited_once()
        kwargs = create_dispatcher.call_args.kwargs
        assert kwargs["repository"] is repository
        assert isinstance(kwargs["files"], LocalFileStorage)
        assert isinstance(kwargs["reader"], OpenpyxlSheetReader)
        assert kwargs["mode"] == DataMode.DEMO
        assert kwargs["business_utc_offset_hours"] == 3
        commands = bot.set_my_commands.call_args.args[0]
        assert [command.command for command in commands] == [
            "start",
            "help",
            "about",
            "inn",
            "check",
            "status",
            "report",
            "cancel",
        ]
        assert bot.set_my_commands.call_args.kwargs["scope"].type == "all_private_chats"
        dispatcher.start_polling.assert_awaited_once_with(
            bot, allowed_updates=["message"], close_bot_session=False
        )
    bot.session.close.assert_awaited_once()
    if failure in (None, "polling"):
        open_repository.assert_called_once_with(Path("x/claims.sqlite3"))
        repository.close.assert_called_once()
    else:
        open_repository.assert_not_called()


async def test_storage_failure_stops_startup_before_polling(monkeypatch):
    bot = SimpleNamespace(
        get_me=AsyncMock(),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
        set_my_commands=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    dispatcher = SimpleNamespace(start_polling=AsyncMock())
    monkeypatch.setattr(app, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(app, "create_dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(
        app, "open_sqlite_repository", Mock(side_effect=RepositoryError("disk full"))
    )
    with pytest.raises(RepositoryError):
        await app.run(Settings(TOKEN, frozenset({42})))
    dispatcher.start_polling.assert_not_awaited()
    bot.session.close.assert_awaited_once()


def test_missing_configuration_exits_before_network(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    bot_constructor = Mock()
    monkeypatch.setattr(app, "Bot", bot_constructor)
    assert app.main() == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    bot_constructor.assert_not_called()


async def test_checko_provider_is_selected_without_network(monkeypatch):
    from claims_assistant.infrastructure.checko.company_data import CheckoCompanyDataProvider

    bot = SimpleNamespace(
        get_me=AsyncMock(),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
        set_my_commands=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    create = Mock(return_value=SimpleNamespace(start_polling=AsyncMock()))
    monkeypatch.setattr(app, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(app, "create_dispatcher", create)
    repository = SimpleNamespace(close=Mock(), recover_interrupted=AsyncMock(return_value=()))
    monkeypatch.setattr(app, "open_sqlite_repository", Mock(return_value=repository))
    await app.run(
        Settings(TOKEN, frozenset({42}), data_provider="checko", checko_api_key="synthetic-key")
    )
    assert isinstance(create.call_args.args[1].inner, CheckoCompanyDataProvider)
    bot.session.close.assert_awaited_once()


def test_safe_formatter_redacts_checko_key():
    record = logging.LogRecord("test", logging.ERROR, "", 0, "%s", ("synthetic-key",), None)
    assert "synthetic-key" not in app.SafeFormatter(TOKEN, "synthetic-key").format(record)


async def test_worker_runs_alongside_polling_and_stops_with_it(monkeypatch):
    bot = SimpleNamespace(
        get_me=AsyncMock(),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
        set_my_commands=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    seen = []

    async def polling(*args, **kwargs):
        seen.append("polling")
        await asyncio.sleep(0.05)

    dispatcher = SimpleNamespace(start_polling=polling)
    monkeypatch.setattr(app, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(app, "create_dispatcher", Mock(return_value=dispatcher))
    repository = SimpleNamespace(close=Mock(), recover_interrupted=AsyncMock(return_value=()))
    monkeypatch.setattr(app, "open_sqlite_repository", Mock(return_value=repository))

    class Worker:
        def __init__(self, *args, **kwargs):
            self.stopped = False

        async def recover(self):
            return await repository.recover_interrupted()

        async def run_forever(self):
            seen.append("worker")
            while not self.stopped:
                await asyncio.sleep(0.01)

        def stop(self):
            self.stopped = True
            seen.append("stopped")

    pipelines = []
    real_pipeline = app.AnalysisPipeline

    def capture_pipeline(*args, **kwargs):
        pipelines.append((args, kwargs))
        return real_pipeline(*args, **kwargs)

    workers = []

    def capture_worker(*args, **kwargs):
        workers.append((args, kwargs))
        return Worker()

    sweepers = []

    class Sweeper:
        def __init__(self, *args, **kwargs):
            sweepers.append((args, kwargs))
            self.stopped = False

        async def run_forever(self):
            seen.append("sweeper")
            while not self.stopped:
                await asyncio.sleep(0.01)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(app, "AnalysisPipeline", capture_pipeline)
    monkeypatch.setattr(app, "RunWorker", capture_worker)
    monkeypatch.setattr(app, "RetentionSweeper", Sweeper)
    await asyncio.wait_for(app.run(Settings(TOKEN, frozenset({42}))), timeout=2)
    assert set(seen[:3]) == {"worker", "polling", "sweeper"}
    assert seen[-1] == "stopped"
    # The retention sweep (S6-02) runs next to the worker with the configured period.
    assert sweepers[0][0][0] is repository
    assert sweepers[0][1]["retention"] == timedelta(days=30)
    assert sweepers[0][1]["interval"] == 6 * 3600
    repository.close.assert_called_once()
    # The pipeline stores steps in the repository, serves the service mode explicitly and
    # takes the run budget from settings; the worker notifies the owner through Telegram.
    (files, reader, provider, steps), options = pipelines[0]
    assert steps is repository
    assert isinstance(provider, app.GuardedCompanyDataProvider)
    assert options["mode"] is app.DataMode.DEMO
    assert options["limits"] == app.RunLimits(max_requests=1500, max_seconds=900.0)
    assert isinstance(workers[0][1]["notifier"], app.TelegramRunNotifier)
