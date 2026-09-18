"""Telegram dialog: new check → analysis date → .xlsx → summary → launch / status."""

from datetime import date

import pytest
from aiogram.methods import SendDocument, SendMessage

from claims_assistant.domain.analysis import RunStatus
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.handlers import create_dispatcher

OWNER = 42
MAIN_MENU = ["Проверить ИНН", "Новая проверка", "Статус", "О сервисе", "Помощь"]


def buttons(reply) -> list[str]:
    return [button.text for row in reply.reply_markup.keyboard for button in row]


def messages(bot) -> list[SendMessage]:
    return [call for call in bot.session.calls if isinstance(call, SendMessage)]


@pytest.fixture
def setup(tmp_path):
    repository = InMemoryAnalysisRepository()
    dispatcher = create_dispatcher(
        frozenset({OWNER, 43}),
        repository=repository,
        files=LocalFileStorage(tmp_path / "uploads"),
        reader=OpenpyxlSheetReader(),
    )
    return dispatcher, repository


async def send(dispatcher, bot, update_factory, text: str, user_id: int = OWNER):
    await dispatcher.feed_update(bot, update_factory(text, user_id=user_id))
    return messages(bot)[-1]


async def send_document(
    dispatcher, bot, update_factory, data: bytes, name="list.xlsx", size=None, user_id=OWNER
):
    bot.session.files[name] = data
    document = {
        "file_id": name,
        "file_unique_id": name,
        "file_name": name,
        "file_size": len(data) if size is None else size,
    }
    await dispatcher.feed_update(bot, update_factory(None, user_id=user_id, document=document))
    return messages(bot)[-1]


async def start_check(dispatcher, bot, update_factory, day="01.09.2026"):
    await send(dispatcher, bot, update_factory, "Новая проверка")
    return await send(dispatcher, bot, update_factory, day)


@pytest.mark.parametrize("entry", ["Новая проверка", "/check"])
async def test_new_check_asks_for_the_analysis_date(setup, bot, update_factory, entry):
    dispatcher, _ = setup
    reply = await send(dispatcher, bot, update_factory, entry)
    assert reply.text == texts.CHECK_DATE_PROMPT
    assert buttons(reply) == ["Сегодня", "Отмена"]


async def test_main_menu_lists_the_new_buttons(setup, bot, update_factory):
    dispatcher, _ = setup
    assert buttons(await send(dispatcher, bot, update_factory, "/start")) == MAIN_MENU


async def test_date_then_template_and_file_prompt(setup, bot, update_factory):
    dispatcher, _ = setup
    reply = await start_check(dispatcher, bot, update_factory, "2026-09-01")
    assert reply.text == texts.CHECK_FILE_PROMPT
    assert buttons(reply) == ["Отмена"]
    sent = [call for call in bot.session.calls if isinstance(call, SendDocument)]
    assert len(sent) == 1 and sent[0].document.filename.endswith(".xlsx")


@pytest.mark.parametrize("raw", ["вчера", "32.13.2026", "01/09/2026", "2026-9-1"])
async def test_bad_date_is_explained_and_asked_again(setup, bot, update_factory, raw):
    dispatcher, _ = setup
    reply = await start_check(dispatcher, bot, update_factory, raw)
    assert reply.text == texts.CHECK_DATE_INVALID
    assert buttons(reply) == ["Сегодня", "Отмена"]


async def test_today_button_uses_the_current_date(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory, "Сегодня")
    reply = await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    # The template's cut-off (01.09.2026) does not match today, so rows carry errors but
    # the run is still created with today's date.
    run = (await repository.list_runs(OWNER))[0]
    assert run.analysis_date == date.today()
    assert texts.CHECK_SUMMARY_TITLE in reply.text


async def test_valid_file_gives_a_summary_with_launch_button(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    reply = await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    assert texts.CHECK_SUMMARY_TITLE in reply.text
    assert "Организаций: 2" in reply.text
    assert "01.09.2026" in reply.text
    assert buttons(reply) == ["Запустить проверку", "Отмена"]
    run = (await repository.list_runs(OWNER))[0]
    assert run.status == RunStatus.DRAFT and len(run.files) == 1


async def test_launch_queues_the_run_and_returns_to_the_menu(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    reply = await send(dispatcher, bot, update_factory, "Запустить проверку")
    assert reply.text.startswith(texts.CHECK_QUEUED_PREFIX)
    assert buttons(reply) == MAIN_MENU
    run = (await repository.list_runs(OWNER))[0]
    assert run.status == RunStatus.QUEUED
    # The dialog is over: the launch phrase now falls back to the menu hint.
    assert (
        await send(dispatcher, bot, update_factory, "Запустить проверку")
    ).text == texts.FALLBACK


async def test_cancel_after_upload_keeps_the_draft_unlaunched(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    reply = await send(dispatcher, bot, update_factory, "Отмена")
    assert reply.text == texts.CHECK_CANCELLED
    assert buttons(reply) == MAIN_MENU
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.DRAFT


async def test_cancel_before_upload_creates_nothing(setup, bot, update_factory):
    dispatcher, repository = setup
    await send(dispatcher, bot, update_factory, "/check")
    reply = await send(dispatcher, bot, update_factory, "/cancel")
    assert reply.text == texts.CHECK_CANCELLED
    assert await repository.list_runs(OWNER) == ()


async def test_same_file_twice_is_reported_not_duplicated(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    data = build_counterparties_template()
    await send_document(dispatcher, bot, update_factory, data, name="a.xlsx")
    reply = await send_document(dispatcher, bot, update_factory, data, name="b.xlsx")
    assert texts.CHECK_DUPLICATE in reply.text
    assert len((await repository.list_runs(OWNER))[0].files) == 1


@pytest.mark.parametrize(
    ("name", "size", "expected"),
    [
        ("list.xls", None, texts.FILE_NOT_XLSX),
        ("list.csv", None, texts.FILE_NOT_XLSX),
        ("list.xlsx", 10 * 1024 * 1024 + 1, texts.FILE_TOO_LARGE),
    ],
)
async def test_wrong_type_or_size_is_rejected_before_download(
    setup, bot, update_factory, name, size, expected
):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    data = build_counterparties_template()
    reply = await send_document(dispatcher, bot, update_factory, data, name=name, size=size)
    assert reply.text == expected
    assert buttons(reply) == ["Отмена"]
    assert all(type(call).__name__ != "GetFile" for call in bot.session.calls)
    assert await repository.list_runs(OWNER) == ()


async def test_corrupt_file_is_explained_and_dialog_continues(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    reply = await send_document(dispatcher, bot, update_factory, b"definitely not xlsx")
    assert texts.CHECK_REJECTED_TITLE in reply.text
    assert "не является корректным .xlsx" in reply.text
    assert buttons(reply) == ["Отмена"]
    assert await repository.list_runs(OWNER) == ()
    reply = await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    assert texts.CHECK_SUMMARY_TITLE in reply.text


async def test_row_issues_are_listed_with_coordinates(setup, bot, update_factory):
    from decimal import Decimal

    from claims_assistant.domain.counterparties import CounterpartyRow

    dispatcher, _ = setup
    await start_check(dispatcher, bot, update_factory)
    rows = (
        CounterpartyRow(inn="1234567894"),
        CounterpartyRow(inn="1234567894", debt=Decimal("5.00"), cutoff_date=date(2026, 9, 1)),
    )
    reply = await send_document(
        dispatcher, bot, update_factory, build_counterparties_template(rows)
    )
    assert "Организаций: 1" in reply.text
    assert "строка 3" in reply.text
    assert "1234567894" not in reply.text


async def test_status_shows_the_latest_run_or_a_hint(setup, bot, update_factory):
    dispatcher, _ = setup
    assert (await send(dispatcher, bot, update_factory, "/status")).text == texts.STATUS_EMPTY
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    await send(dispatcher, bot, update_factory, "Запустить проверку")
    reply = await send(dispatcher, bot, update_factory, "Статус")
    assert texts.STATUS_TITLE in reply.text
    assert "01.09.2026" in reply.text
    assert texts.status_label(RunStatus.QUEUED) in reply.text
    assert buttons(reply) == MAIN_MENU


async def test_status_is_owner_scoped(setup, bot, update_factory):
    dispatcher, _ = setup
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    reply = await send(dispatcher, bot, update_factory, "/status", user_id=43)
    assert reply.text == texts.STATUS_EMPTY


async def test_document_outside_the_dialog_is_still_unsupported(setup, bot, update_factory):
    dispatcher, repository = setup
    reply = await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    assert reply.text == texts.FILE_UNSUPPORTED
    assert await repository.list_runs(OWNER) == ()


async def test_menu_commands_leave_the_check_dialog(setup, bot, update_factory):
    dispatcher, _ = setup
    await send(dispatcher, bot, update_factory, "/check")
    assert (await send(dispatcher, bot, update_factory, "/help")).text == texts.HELP
    assert (await send(dispatcher, bot, update_factory, "01.09.2026")).text == texts.FALLBACK


async def test_unlisted_user_cannot_start_a_check(setup, bot, update_factory):
    dispatcher, _ = setup
    reply = await send(dispatcher, bot, update_factory, "/check", user_id=99)
    assert reply.text == texts.access_denied(99)
