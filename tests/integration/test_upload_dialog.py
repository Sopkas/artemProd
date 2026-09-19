"""Telegram dialog: new check → analysis date → .xlsx → summary → launch / status."""

from datetime import date, datetime, timedelta, timezone

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
MAIN_MENU = ["Проверить ИНН", "Новая проверка", "Статус", "Отчёт", "О сервисе", "Помощь"]


LAUNCH_MENU = [
    "Запустить проверку",
    "Добавить платежи",
    "Добавить историю долга",
    "Добавить взаимодействия",
    "Отмена",
]


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
    # «Сегодня» is the calendar date in the business timezone (Moscow by default),
    # not the UTC date and not the machine's local date.
    assert run.analysis_date == datetime.now(timezone(timedelta(hours=3))).date()
    assert texts.CHECK_SUMMARY_TITLE in reply.text


async def test_today_follows_the_configured_business_timezone(bot, update_factory, tmp_path):
    repository = InMemoryAnalysisRepository()
    dispatcher = create_dispatcher(
        frozenset({OWNER}),
        repository=repository,
        files=LocalFileStorage(tmp_path / "uploads"),
        reader=OpenpyxlSheetReader(),
        business_utc_offset_hours=10,
    )
    await start_check(dispatcher, bot, update_factory, "Сегодня")
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    run = (await repository.list_runs(OWNER))[0]
    assert run.analysis_date == datetime.now(timezone(timedelta(hours=10))).date()


async def test_valid_file_gives_a_summary_with_launch_button(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    reply = await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    assert texts.CHECK_SUMMARY_TITLE in reply.text
    assert "Организаций: 2" in reply.text
    assert "01.09.2026" in reply.text
    assert buttons(reply) == LAUNCH_MENU
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


async def test_status_shows_the_failure_reason_after_processing(setup, bot, update_factory):
    from claims_assistant.application.analysis_queue import RunOutcome

    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    await send(dispatcher, bot, update_factory, "Запустить проверку")
    run = await repository.claim_next()
    await repository.finish(run.id, RunOutcome(RunStatus.PARTIAL, "Строк с ошибками: 1"))
    reply = await send(dispatcher, bot, update_factory, "/status")
    assert texts.status_label(RunStatus.PARTIAL) in reply.text
    assert "Строк с ошибками: 1" in reply.text


# --- S3-03: a finished report is sent again without a new check ---


async def _finished_run_with_report(setup, bot, update_factory, tmp_path):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    await send(dispatcher, bot, update_factory, "Запустить проверку")
    run = await repository.claim_next()
    from claims_assistant.application.analysis_queue import RunOutcome

    await repository.finish(run.id, RunOutcome(RunStatus.COMPLETED))
    stored_path = LocalFileStorage(tmp_path / "uploads").save(run.id, b"PK-report-bytes")
    await repository.save_report(run.id, stored_path)
    return dispatcher, repository, run


@pytest.mark.parametrize("entry", ["/report", "Отчёт"])
async def test_report_is_sent_and_delivery_recorded(setup, bot, update_factory, tmp_path, entry):
    from claims_assistant.domain.steps import DeliveryStatus

    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    calls_before = len(bot.session.calls)
    await dispatcher.feed_update(bot, update_factory(entry, user_id=OWNER))
    sent = [c for c in bot.session.calls[calls_before:] if isinstance(c, SendDocument)]
    assert len(sent) == 1
    assert sent[0].document.filename.endswith(".xlsx")
    assert sent[0].document.data == b"PK-report-bytes"
    report = await repository.get_report(OWNER, run.id)
    assert report.delivery is DeliveryStatus.DELIVERED
    # No new run was created or queued by resending.
    assert len(await repository.list_runs(OWNER)) == 1
    assert (await repository.get_run(OWNER, run.id)).status == RunStatus.COMPLETED
    assert await repository.claim_next() is None


async def test_report_of_the_previous_check_stays_available_after_a_new_one_starts(
    setup, bot, update_factory, tmp_path
):
    """Review B on #23: a draft or queued check started later must not hide the report."""
    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    await start_check(dispatcher, bot, update_factory)
    await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    assert (await repository.list_runs(OWNER))[0].id != run.id  # the draft is newest now
    calls_before = len(bot.session.calls)
    await dispatcher.feed_update(bot, update_factory("/report", user_id=OWNER))
    sent = [c for c in bot.session.calls[calls_before:] if isinstance(c, SendDocument)]
    assert len(sent) == 1 and sent[0].document.data == b"PK-report-bytes"
    assert sent[0].caption == texts.report_caption(run)
    assert run.analysis_date.strftime("%d.%m.%Y") in sent[0].caption
    # The draft is untouched: still a draft, still the newest run, nothing queued.
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.DRAFT
    assert await repository.claim_next() is None


async def test_send_failure_asks_to_retry_and_marks_delivery_failed(
    setup, bot, update_factory, tmp_path, caplog
):
    from claims_assistant.domain.steps import DeliveryStatus

    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    bot.session.fail_once = RuntimeError("telegram network error with private text")
    reply = await send(dispatcher, bot, update_factory, "/report")
    assert reply.text == texts.REPORT_SEND_FAILED
    assert buttons(reply) == MAIN_MENU
    assert (await repository.get_report(OWNER, run.id)).delivery is DeliveryStatus.FAILED
    assert "report_delivery_failed" in caplog.text and "private text" not in caplog.text
    # The file is intact: the next /report succeeds.
    calls_before = len(bot.session.calls)
    await dispatcher.feed_update(bot, update_factory("/report", user_id=OWNER))
    assert any(isinstance(c, SendDocument) for c in bot.session.calls[calls_before:])
    assert (await repository.get_report(OWNER, run.id)).delivery is DeliveryStatus.DELIVERED


async def test_report_can_be_sent_twice(setup, bot, update_factory, tmp_path):
    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    await dispatcher.feed_update(bot, update_factory("/report", user_id=OWNER))
    await dispatcher.feed_update(bot, update_factory("/report", user_id=OWNER))
    assert len([c for c in bot.session.calls if isinstance(c, SendDocument)]) >= 3  # template + 2


async def test_report_without_a_finished_run_explains(setup, bot, update_factory):
    dispatcher, _ = setup
    reply = await send(dispatcher, bot, update_factory, "/report")
    assert reply.text == texts.REPORT_EMPTY
    assert buttons(reply) == MAIN_MENU


async def test_report_is_owner_scoped(setup, bot, update_factory, tmp_path):
    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    reply = await send(dispatcher, bot, update_factory, "/report", user_id=43)
    assert reply.text == texts.REPORT_EMPTY


async def test_missing_report_file_marks_delivery_failed(setup, bot, update_factory, tmp_path):
    from claims_assistant.domain.steps import DeliveryStatus

    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    report = await repository.get_report(OWNER, run.id)
    LocalFileStorage(tmp_path / "uploads").remove(report.stored_path)
    reply = await send(dispatcher, bot, update_factory, "/report")
    assert reply.text == texts.REPORT_UNAVAILABLE
    assert (await repository.get_report(OWNER, run.id)).delivery is DeliveryStatus.FAILED
    assert (await repository.get_run(OWNER, run.id)).status == RunStatus.COMPLETED


async def test_status_mentions_the_report_when_it_exists(setup, bot, update_factory, tmp_path):
    dispatcher, repository, run = await _finished_run_with_report(
        setup, bot, update_factory, tmp_path
    )
    reply = await send(dispatcher, bot, update_factory, "/status")
    assert texts.STATUS_REPORT_HINT in reply.text


# --- S4-01: optional files of the package ---


def payments_file(rows):
    from claims_assistant.infrastructure.excel.ledgers import build_payments_workbook

    return build_payments_workbook(rows)


def history_file(rows):
    from claims_assistant.infrastructure.excel.ledgers import build_debt_history_workbook

    return build_debt_history_workbook(rows)


INN_1, INN_2 = "7707083893", "7710140679"  # the two INNs of the counterparties template


async def package_ready(setup, bot, update_factory):
    dispatcher, repository = setup
    await start_check(dispatcher, bot, update_factory)
    reply = await send_document(dispatcher, bot, update_factory, build_counterparties_template())
    assert "Состав пакета:" in reply.text and "• Контрагенты — организаций: 2" in reply.text
    return dispatcher, repository


async def test_payments_need_a_period_then_a_file_and_show_the_composition(
    setup, bot, update_factory
):
    from claims_assistant.domain.analysis import FileKind
    from claims_assistant.domain.external import Period

    dispatcher, repository = await package_ready(setup, bot, update_factory)
    reply = await send(dispatcher, bot, update_factory, "Добавить платежи")
    assert reply.text == texts.PAYMENTS_PERIOD_PROMPT and buttons(reply) == ["Отмена"]

    documents_before = len([c for c in bot.session.calls if isinstance(c, SendDocument)])
    reply = await send(dispatcher, bot, update_factory, "01.06.2026 – 31.08.2026")
    assert reply.text == texts.PAYMENTS_FILE_PROMPT
    sent = [c for c in bot.session.calls if isinstance(c, SendDocument)]
    assert len(sent) == documents_before + 1 and sent[-1].document.filename == "platezhi.xlsx"

    data = payments_file(
        [[INN_1, "P-1", date(2026, 7, 15), 100.0], [INN_2, "P-2", date(2026, 8, 1), 5.0]]
    )
    reply = await send_document(dispatcher, bot, update_factory, data, name="pay.xlsx")
    assert "Файл «Платежи» принят" in reply.text
    assert "Строк принято: 2" in reply.text
    assert "• Платежи — строк: 2, период 01.06.2026–31.08.2026" in reply.text
    assert "• Контрагенты — организаций: 2" in reply.text
    assert buttons(reply) == LAUNCH_MENU

    run = (await repository.list_runs(OWNER))[0]
    assert [file.kind for file in run.files] == [FileKind.COUNTERPARTIES, FileKind.PAYMENTS]
    assert run.files[1].coverage == Period(date(2026, 6, 1), date(2026, 8, 31))
    assert run.status == RunStatus.DRAFT


@pytest.mark.parametrize(
    "text",
    [
        "01.06.2026",
        "31.08.2026–01.06.2026",
        "01.06.2026–02.09.2026",
        "вчера–сегодня",
        "01.06.2026–31.08.2026–01.09.2026",
    ],
)
async def test_bad_period_is_asked_again(setup, bot, update_factory, text):
    dispatcher, _ = await package_ready(setup, bot, update_factory)
    await send(dispatcher, bot, update_factory, "Добавить платежи")
    reply = await send(dispatcher, bot, update_factory, text)
    assert reply.text == texts.PAYMENTS_PERIOD_INVALID


async def test_debt_history_is_attached_without_a_period(setup, bot, update_factory):
    from claims_assistant.domain.analysis import FileKind

    dispatcher, repository = await package_ready(setup, bot, update_factory)
    reply = await send(dispatcher, bot, update_factory, "Добавить историю долга")
    assert reply.text == texts.HISTORY_FILE_PROMPT
    last_document = [c for c in bot.session.calls if isinstance(c, SendDocument)][-1]
    assert last_document.document.filename == "istoriya-dolga.xlsx"
    data = history_file([[INN_1, date(2026, 8, 1), 100.0], [INN_1, date(2026, 9, 1), 120.0]])
    reply = await send_document(dispatcher, bot, update_factory, data, name="hist.xlsx")
    assert "Файл «История долга» принят" in reply.text
    assert "• История долга — строк: 2\n" in reply.text + "\n"
    run = (await repository.list_runs(OWNER))[0]
    assert [f.kind for f in run.files] == [FileKind.COUNTERPARTIES, FileKind.DEBT_HISTORY]
    assert run.files[1].coverage is None


async def test_ledger_with_only_foreign_inns_is_rejected_and_not_stored(setup, bot, update_factory):
    dispatcher, repository = await package_ready(setup, bot, update_factory)
    await send(dispatcher, bot, update_factory, "Добавить историю долга")
    data = history_file([["1234567894", date(2026, 8, 1), 100.0]])
    reply = await send_document(dispatcher, bot, update_factory, data, name="hist.xlsx")
    assert "не принят" in reply.text and "нет в файле «Контрагенты»" in reply.text
    assert buttons(reply) == ["Отмена"]
    assert len((await repository.list_runs(OWNER))[0].files) == 1
    # The user can still cancel back to the package and launch it.
    reply = await send(dispatcher, bot, update_factory, "Отмена")
    assert reply.text == texts.LEDGER_CANCELLED and buttons(reply) == LAUNCH_MENU
    reply = await send(dispatcher, bot, update_factory, "Запустить проверку")
    assert reply.text.startswith(texts.CHECK_QUEUED_PREFIX)
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.QUEUED


async def test_cancel_during_the_period_keeps_the_package(setup, bot, update_factory):
    dispatcher, repository = await package_ready(setup, bot, update_factory)
    await send(dispatcher, bot, update_factory, "Добавить платежи")
    reply = await send(dispatcher, bot, update_factory, "/cancel")
    assert reply.text == texts.LEDGER_CANCELLED
    reply = await send(dispatcher, bot, update_factory, "Отмена")
    assert reply.text == texts.CHECK_CANCELLED  # cancelling from the package drops the dialog
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.DRAFT


async def test_same_ledger_twice_is_reported_not_duplicated(setup, bot, update_factory):
    dispatcher, repository = await package_ready(setup, bot, update_factory)
    data = history_file([[INN_1, date(2026, 8, 1), 100.0]])
    for _ in range(2):
        await send(dispatcher, bot, update_factory, "Добавить историю долга")
        reply = await send_document(dispatcher, bot, update_factory, data, name="hist.xlsx")
    assert texts.CHECK_DUPLICATE in reply.text
    assert len((await repository.list_runs(OWNER))[0].files) == 2


async def test_launch_with_a_full_package_queues_all_files(setup, bot, update_factory):
    dispatcher, repository = await package_ready(setup, bot, update_factory)
    await send(dispatcher, bot, update_factory, "Добавить платежи")
    await send(dispatcher, bot, update_factory, "01.06.2026–31.08.2026")
    payments = payments_file([[INN_1, "P-1", date(2026, 7, 15), 100.0]])
    await send_document(dispatcher, bot, update_factory, payments, name="p.xlsx")
    await send(dispatcher, bot, update_factory, "Добавить историю долга")
    history = history_file([[INN_1, date(2026, 8, 1), 100.0]])
    await send_document(dispatcher, bot, update_factory, history, name="h.xlsx")
    reply = await send(dispatcher, bot, update_factory, "Запустить проверку")
    assert "файлов 3" in reply.text
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.QUEUED


@pytest.mark.parametrize(
    "text, expected",
    [
        ("01.06.2026–31.08.2026", (date(2026, 6, 1), date(2026, 8, 31))),
        ("01.06.2026 - 31.08.2026", (date(2026, 6, 1), date(2026, 8, 31))),
        ("01.06.2026-31.08.2026", (date(2026, 6, 1), date(2026, 8, 31))),
        ("2026-06-01 2026-09-01", (date(2026, 6, 1), date(2026, 9, 1))),
        ("01.06.2026 — 01.06.2026", (date(2026, 6, 1), date(2026, 6, 1))),
        ("01.06.2026", None),
        ("02.09.2026–03.09.2026", None),  # after the analysis date
        ("31.08.2026–01.06.2026", None),
        ("сегодня–сегодня", None),  # «Сегодня» is not accepted inside a period
    ],
)
def test_parse_period(text, expected):
    from claims_assistant.domain.external import Period
    from claims_assistant.presentation.telegram.handlers import parse_period

    period = parse_period(text, date(2026, 9, 1))
    assert period == (None if expected is None else Period(*expected))


# --- S4-02: interactions ---


def interactions_file(rows):
    from claims_assistant.infrastructure.excel.ledgers import build_interactions_workbook

    return build_interactions_workbook(rows)


async def test_interactions_are_attached_and_counted(setup, bot, update_factory):
    from claims_assistant.domain.analysis import FileKind

    dispatcher, repository = await package_ready(setup, bot, update_factory)
    reply = await send(dispatcher, bot, update_factory, "Добавить взаимодействия")
    assert reply.text == texts.INTERACTIONS_FILE_PROMPT
    last_document = [c for c in bot.session.calls if isinstance(c, SendDocument)][-1]
    assert last_document.document.filename == "vzaimodeystviya.xlsx"
    data = interactions_file(
        [
            [INN_1, "I-2", date(2026, 8, 20), "Обещали оплатить до конца месяца.", "телефон"],
            [INN_1, "I-1", date(2026, 8, 1), "Направлена претензия.", None],
            ["1234567894", "I-9", date(2026, 8, 1), "Чужой контрагент.", None],
        ]
    )
    reply = await send_document(dispatcher, bot, update_factory, data, name="int.xlsx")
    assert "Файл «Взаимодействия» принят" in reply.text
    assert "Строк принято: 2" in reply.text and "Ошибок: 1" in reply.text
    assert "• Взаимодействия — строк: 2" in reply.text
    # Comments never come back in the chat.
    assert "претензия" not in reply.text and "Чужой" not in reply.text
    run = (await repository.list_runs(OWNER))[0]
    assert [f.kind for f in run.files] == [FileKind.COUNTERPARTIES, FileKind.INTERACTIONS]
    assert run.files[1].coverage is None


# --- S4-03: the package as a whole ---


async def test_second_different_counterparties_file_is_refused_with_an_explanation(
    setup, bot, update_factory
):
    from claims_assistant.application.check_package import MAIN_FILE_ALREADY_IN_PACKAGE
    from claims_assistant.domain.counterparties import CounterpartyRow

    dispatcher, repository = await package_ready(setup, bot, update_factory)
    other = build_counterparties_template(
        (CounterpartyRow(inn=INN_1, cutoff_date=date(2026, 9, 1)),)
    )
    reply = await send_document(dispatcher, bot, update_factory, other, name="other.xlsx")
    assert reply.text == texts.package_conflict(MAIN_FILE_ALREADY_IN_PACKAGE)
    assert buttons(reply) == LAUNCH_MENU
    assert len((await repository.list_runs(OWNER))[0].files) == 1


async def test_launch_reports_cross_file_findings_once(setup, bot, update_factory):
    dispatcher, repository = await package_ready(setup, bot, update_factory)
    same = [INN_1, "P-1", date(2026, 7, 15), 100.0]
    for name in ("p1.xlsx", "p2.xlsx"):
        await send(dispatcher, bot, update_factory, "Добавить платежи")
        await send(dispatcher, bot, update_factory, "01.06.2026–31.08.2026")
        rows = [same] if name == "p1.xlsx" else [same, [INN_1, "P-9", date(2026, 8, 1), 1.0]]
        await send_document(dispatcher, bot, update_factory, payments_file(rows), name=name)
    reply = await send(dispatcher, bot, update_factory, "Запустить проверку")
    assert reply.text.startswith(texts.PACKAGE_REVIEW_TITLE)
    assert "повторяется в другом файле пакета" in reply.text
    assert texts.CHECK_QUEUED_PREFIX in reply.text
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.QUEUED


async def test_launch_is_blocked_when_the_package_is_malformed(setup, bot, update_factory):
    from claims_assistant.application.analysis_repository import NewFile
    from claims_assistant.domain.analysis import FileKind

    dispatcher, repository = await package_ready(setup, bot, update_factory)
    run = (await repository.list_runs(OWNER))[0]
    await repository.add_file(
        OWNER,
        run.id,
        NewFile(
            kind=FileKind.COUNTERPARTIES,
            checksum="c" * 64,
            size_bytes=5,
            stored_path=f"{run.id}/second.xlsx",
        ),
    )
    reply = await send(dispatcher, bot, update_factory, "Запустить проверку")
    assert reply.text.startswith(texts.PACKAGE_BLOCKED_TITLE)
    assert "больше одного файла «Контрагенты»" in reply.text
    assert buttons(reply) == LAUNCH_MENU
    assert (await repository.list_runs(OWNER))[0].status == RunStatus.DRAFT
