"""S8-09 (part A): the «Мониторинг» button and the notices, through the real dispatcher.

The list lives in a real SQLite file; the source is never asked here — the daily run is
covered in ``tests/unit/test_monitoring_service.py``. INNs are synthetic, built for the
check digit.
"""

from datetime import UTC, date, datetime
from io import BytesIO

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendDocument, SendMessage
from aiogram.types import InlineKeyboardMarkup, Update
from openpyxl import Workbook

from claims_assistant.application.monitoring import (
    CheckRecord,
    MonitoringPolicy,
    MonitoringService,
)
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.monitoring import Comparison
from claims_assistant.domain.watchlist import Candidate
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.persistence.watch import SqliteWatchStore
from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.handlers import create_dispatcher
from claims_assistant.presentation.telegram.monitoring_dialog import names_in
from claims_assistant.presentation.telegram.monitoring_notifier import (
    TelegramMonitoringNotifier,
    TelegramMonitoringTexts,
)

OWNER = 42
MAIN_MENU = [
    "Проверить ИНН",
    "Мониторинг",
    "Новая проверка",
    "Статус",
    "Отчёт",
    "О сервисе",
    "Помощь",
]
WATCH_MENU = [
    "Добавить на мониторинг",
    "Убрать из мониторинга",
    "Список файлом",
    "Очистить весь список",
    "В главное меню",
]
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def inn10(nine: str) -> str:
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return nine + str(sum(int(d) * w for d, w in zip(nine, weights, strict=True)) % 11 % 10)


A = inn10("123456789")
B = inn10("770000001")


class Source:
    def scoped(self, budget):
        return DemoCompanyDataProvider()


@pytest.fixture
def store(tmp_path):
    repository = open_sqlite_repository(tmp_path / "claims.sqlite3")
    yield SqliteWatchStore(repository.engine, lambda: NOW)
    repository.close()


@pytest.fixture
def dispatcher(store):
    service = MonitoringService(
        store,
        Source(),
        TelegramMonitoringTexts(),
        mode=DataMode.DEMO,
        policy=MonitoringPolicy(),
        clock=lambda: NOW,
    )
    return create_dispatcher(
        frozenset({OWNER, 43}), reader=OpenpyxlSheetReader(), monitoring=service
    )


def buttons(reply: SendMessage) -> list[str]:
    return [button.text for row in reply.reply_markup.keyboard for button in row]


async def send(dispatcher, bot, update_factory, *items: str, user_id: int = OWNER) -> SendMessage:
    for text in items:
        await dispatcher.feed_update(bot, update_factory(text, user_id=user_id))
    return bot.session.last_message


async def send_file(dispatcher, bot, update_factory, data: bytes, name: str = "list.xlsx"):
    bot.session.files[name] = data
    document = {"file_id": name, "file_unique_id": name, "file_name": name, "file_size": len(data)}
    await dispatcher.feed_update(bot, update_factory(None, user_id=OWNER, document=document))
    return bot.session.last_message


async def tap(dispatcher, bot, data: str, user_id: int = OWNER) -> None:
    update = Update(
        update_id=2,
        callback_query={
            "id": "tap",
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat_instance": "chat",
            "data": data,
            "message": {
                "message_id": 100,
                "date": NOW,
                "chat": {"id": user_id, "type": "private"},
                "text": "🔔 Мониторинг",
            },
        },
    )
    await dispatcher.feed_update(bot, update)


def workbook(rows: list[list]) -> bytes:
    book = Workbook()
    for row in rows:
        book.active.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def calls(bot, kind):
    return [call for call in bot.session.calls if isinstance(call, kind)]


# --- the screen and the main menu ---


async def test_the_button_stands_next_to_the_card_in_the_main_menu(bot, update_factory, dispatcher):
    assert buttons(await send(dispatcher, bot, update_factory, "/start")) == MAIN_MENU


@pytest.mark.parametrize("entry", ["Мониторинг", "/monitor"])
async def test_an_empty_list_says_what_monitoring_does(bot, update_factory, dispatcher, entry):
    reply = await send(dispatcher, bot, update_factory, entry)
    assert reply.text.startswith("🔔 Мониторинг: в списке пока никого нет.")
    assert "Проверка — каждый день в 09:00 по Москве." in reply.text
    assert "от 100 000 ₽" in reply.text
    assert buttons(reply) == WATCH_MENU


async def test_without_the_service_the_button_says_monitoring_is_off(bot, update_factory):
    reply = await send(create_dispatcher(frozenset({OWNER})), bot, update_factory, "Мониторинг")
    assert reply.text == texts.WATCH_UNAVAILABLE
    assert buttons(reply) == MAIN_MENU


async def test_back_and_the_shell_buttons_leave_the_dialog(bot, update_factory, dispatcher):
    reply = await send(dispatcher, bot, update_factory, "Мониторинг", "В главное меню")
    assert reply.text == texts.BACK_TO_MENU and buttons(reply) == MAIN_MENU
    # «Статус» while adding is the shell's: not a list without INNs.
    reply = await send(
        dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", "Статус"
    )
    assert reply.text == texts.STATUS_EMPTY
    reply = await send(dispatcher, bot, update_factory, A)
    assert reply.text == texts.FALLBACK


# --- adding ---


async def test_inns_in_a_message_are_added_and_the_screen_shows_them(
    bot, update_factory, dispatcher
):
    reply = await send(
        dispatcher,
        bot,
        update_factory,
        "Мониторинг",
        "Добавить на мониторинг",
        f"1. {A} ООО «Альфа»\n2) {B}, 16.10.2026",
    )
    assert reply.text.startswith("Добавлено на мониторинг: 2.")
    assert "Сейчас на мониторинге: 2 из 100." in reply.text
    assert reply.text.endswith(
        "Проверка — каждый день в 09:00 по Москве. Первая проверка — точка отсчёта: "
        "об изменениях пишу со следующей."
    )
    assert buttons(reply) == WATCH_MENU
    screen = await send(dispatcher, bot, update_factory, "Мониторинг")
    assert screen.text.startswith("🔔 Мониторинг: 2 из 100.")
    assert "Ждут первой проверки: 2." in screen.text
    recent = screen.text.split("Последние добавленные:\n")[1].splitlines()[:2]
    assert recent == [f"  {B}", f"  {A} — ООО «Альфа»"]  # newest first, the name kept


@pytest.mark.parametrize(
    ("text", "names"),
    [
        (f"1. {A} ПАО «Альфа»", {A: "ПАО «Альфа»"}),
        (f"2) {A} — ООО «Бета», 16.10.2026", {A: "ООО «Бета»"}),
        (f"ООО «Гамма» ИНН {A}", {A: "ООО «Гамма»"}),
        (f"ИНН: {A}; ООО «Дельта»", {A: "ООО «Дельта»"}),
        (f"{A}\t{B}", {}),  # two INNs on a line: whose name, unknown
        (f"{A}, 15", {}),  # no letters: no name
        (f"{A} Альфа\n{B}", {A: "Альфа"}),
    ],
)
def test_a_name_is_read_from_the_line_of_its_inn(text, names):
    assert names_in(text) == names


async def test_a_name_beside_an_inn_is_kept_and_fills_a_gap(bot, update_factory, dispatcher):
    """17.10.2026: the names typed beside INNs were dropped, «Название» stayed empty, and
    sending them again changed nothing — the INNs were already on the list."""
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", f"{A}\n{B}")
    screen = await send(dispatcher, bot, update_factory, "Мониторинг")
    assert texts.WATCH_NAME_LATER in screen.text
    reply = await send(
        dispatcher,
        bot,
        update_factory,
        "Добавить на мониторинг",
        f"1. {A} ПАО «Альфа»\n2) {B} — ООО «Бета», 16.10.2026",
    )
    assert "Уже были на мониторинге: 2." in reply.text
    screen = await send(dispatcher, bot, update_factory, "Мониторинг")
    assert f"  {B} — ООО «Бета»" in screen.text and f"  {A} — ПАО «Альфа»" in screen.text
    assert texts.WATCH_NAME_LATER not in screen.text
    # A name typed beside the wrong INN is corrected by sending it again (17.10.2026).
    reply = await send(
        dispatcher, bot, update_factory, "Добавить на мониторинг", f"{A} АО «Альфа-2»\n{B}"
    )
    assert "Уже были на мониторинге: 2." in reply.text
    # B came without a name: kept as it was. The change is shown, so a stray word is seen.
    assert f"Названия обновлены: 1.\n• {A}: ПАО «Альфа» → АО «Альфа-2»" in reply.text
    screen = await send(dispatcher, bot, update_factory, "Мониторинг")
    assert f"  {A} — АО «Альфа-2»" in screen.text and f"  {B} — ООО «Бета»" in screen.text


def test_a_renamed_company_shows_what_it_was_and_the_rest_are_counted():
    renames = [(A, None, "ООО «Альфа»"), (B, "ПАО «Бета»", "тоже проверь")]
    renames += [(inn10(f"77000001{n}"), "Старое", "Новое") for n in (3, 4)]
    assert texts.watch_renamed(renames) == (
        "Названия обновлены: 4.\n"
        f"• {A}: без названия → ООО «Альфа»\n"
        f"• {B}: ПАО «Бета» → тоже проверь\n"
        f"• {renames[2][0]}: Старое → Новое\n"
        "…и ещё 1 — в «Список файлом»."
    )


async def test_a_mistyped_inn_is_named_by_its_place_and_the_rest_is_added(
    bot, update_factory, dispatcher
):
    reply = await send(
        dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", f"{A}, 123456789"
    )
    assert reply.text.startswith("Добавлено на мониторинг: 1.")
    assert "Не ИНН, проверьте (1):" in reply.text and "№ 2:" in reply.text
    assert "123456789" not in reply.text  # the value is never repeated


async def test_a_message_without_inns_is_asked_again(bot, update_factory, dispatcher):
    reply = await send(
        dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", "привет"
    )
    assert reply.text == texts.WATCH_NO_INN and buttons(reply) == ["Отмена"]
    reply = await send(dispatcher, bot, update_factory, A)
    assert reply.text.startswith("Добавлено на мониторинг: 1.")


async def test_a_table_with_an_inn_column_is_added_with_its_names(bot, update_factory, dispatcher):
    data = workbook([["Наименование", "ИНН"], ["ООО «Альфа»", A], ["ООО «Бета»", int(B)]])
    await send(dispatcher, bot, update_factory, "Мониторинг")
    # A file sent straight on the monitoring screen is a list as well.
    reply = await send_file(dispatcher, bot, update_factory, data)
    assert reply.text.startswith("Добавлено на мониторинг: 2.")
    screen = await send(dispatcher, bot, update_factory, "Мониторинг")
    assert f"  {B} — ООО «Бета»" in screen.text and f"  {A} — ООО «Альфа»" in screen.text


async def test_a_file_without_inns_or_of_another_type_is_explained(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг")
    reply = await send_file(dispatcher, bot, update_factory, b"%PDF", name="list.pdf")
    assert reply.text == texts.WATCH_FILE_TYPE
    reply = await send_file(dispatcher, bot, update_factory, workbook([["Название"], ["Альфа"]]))
    assert "колонки «ИНН»" in reply.text and buttons(reply) == ["Отмена"]
    reply = await send_file(dispatcher, bot, update_factory, b"not a workbook")
    assert reply.text.endswith(texts.WATCH_FILE_UNREADABLE)


# --- taking off ---


async def test_inns_are_taken_off_by_a_message(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", f"{A} {B}")
    reply = await send(dispatcher, bot, update_factory, "Убрать из мониторинга", A)
    assert reply.text.startswith("Убрано с мониторинга: 1.")
    assert reply.text.endswith("Сейчас на мониторинге: 1 из 100.")
    assert buttons(reply) == WATCH_MENU


async def test_nothing_to_take_off_on_an_empty_list(bot, update_factory, dispatcher):
    reply = await send(dispatcher, bot, update_factory, "Мониторинг", "Убрать из мониторинга")
    assert reply.text == texts.WATCH_LIST_EMPTY and buttons(reply) == WATCH_MENU


async def test_clearing_asks_first_and_cancel_keeps_the_list(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", f"{A} {B}")
    reply = await send(dispatcher, bot, update_factory, "Очистить весь список")
    assert "весь список — 2 организации?" in reply.text
    assert buttons(reply) == ["Да, очистить список", "Отмена"]
    reply = await send(dispatcher, bot, update_factory, "Отмена")
    assert reply.text == texts.WATCH_STEP_CANCELLED
    reply = await send(
        dispatcher, bot, update_factory, "Очистить весь список", "Да, очистить список"
    )
    assert reply.text == "Список очищен: убрано 2 организации."
    reply = await send(dispatcher, bot, update_factory, "Очистить весь список")
    assert reply.text == texts.WATCH_CLEAR_EMPTY


async def test_the_whole_list_comes_as_a_file(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Список файлом")
    assert bot.session.last_message.text == texts.WATCH_LIST_EMPTY
    await send(dispatcher, bot, update_factory, "Добавить на мониторинг", f"{A} {B}")
    await send(dispatcher, bot, update_factory, "Список файлом")
    [document] = calls(bot, SendDocument)
    assert document.document.filename == "monitoring-2026-10-16.xlsx"
    assert document.caption == "Список на мониторинге: 2 из 100."


async def test_one_users_list_is_not_anothers(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", A)
    reply = await send(dispatcher, bot, update_factory, "Мониторинг", user_id=43)
    assert reply.text.startswith("🔔 Мониторинг: в списке пока никого нет.")


# --- the button under a notice ---


async def test_the_button_under_a_notice_takes_the_company_off(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", f"{A} {B}")
    await tap(dispatcher, bot, f"unwatch:{A}")
    answer = calls(bot, AnswerCallbackQuery)[-1]
    assert answer.text == texts.UNWATCHED_SHORT
    assert (
        bot.session.last_message.text
        == f"ИНН {A} убран с мониторинга. Сейчас на мониторинге: 1 из 100."
    )
    edit = calls(bot, EditMessageReplyMarkup)[-1]
    assert edit.reply_markup is None and edit.message_id == 100
    # A second tap on an old notice: already off, nothing else changes.
    await tap(dispatcher, bot, f"unwatch:{A}")
    assert calls(bot, AnswerCallbackQuery)[-1].text == texts.UNWATCH_ABSENT


async def test_a_tap_from_someone_without_access_is_refused(bot, update_factory, dispatcher):
    await send(dispatcher, bot, update_factory, "Мониторинг", "Добавить на мониторинг", A)
    await tap(dispatcher, bot, f"unwatch:{A}", user_id=99)
    answer = calls(bot, AnswerCallbackQuery)[-1]
    assert answer.show_alert and "Доступ к боту ограничен" in answer.text
    screen = await send(dispatcher, bot, update_factory, "Мониторинг")
    assert screen.text.startswith("🔔 Мониторинг: 1 из 100.")


# --- sending the notices ---


async def queue(store) -> None:
    """A change notice for A and the day's summary, as a run leaves them."""
    day = date(2026, 10, 16)
    await store.add(OWNER, [Candidate(A)], day, 100)
    record = CheckRecord(A, day, None, (), Comparison(A, (), ()), "🔔 Мониторинг, 16.10.2026: …")
    assert await store.record_check(OWNER, record)
    assert await store.queue_summary(OWNER, day, "📋 Мониторинг на 16.10.2026.")


async def test_notices_go_out_in_order_and_a_company_has_its_button(bot, store):
    await queue(store)
    assert await TelegramMonitoringNotifier(bot, store).deliver() == 2
    notice, summary = calls(bot, SendMessage)
    assert notice.chat_id == OWNER and notice.text.startswith("🔔")
    assert isinstance(notice.reply_markup, InlineKeyboardMarkup)
    [[button]] = notice.reply_markup.inline_keyboard
    assert (button.text, button.callback_data) == ("Больше не следить", f"unwatch:{A}")
    assert summary.text.startswith("📋") and summary.reply_markup is None
    assert await store.pending(10) == ()


async def test_without_telegram_the_notices_wait_in_their_order(bot, store):
    await queue(store)
    bot.session.fail_once = TelegramNetworkError(SendMessage(chat_id=OWNER, text="x"), "down")
    notifier = TelegramMonitoringNotifier(bot, store)
    assert await notifier.deliver() == 0
    assert len(await store.pending(10)) == 2
    assert await notifier.deliver() == 2
    texts_sent = [call.text[:1] for call in calls(bot, SendMessage)]
    assert texts_sent == ["🔔", "🔔", "📋"]  # the failed try, then both in order


async def test_a_blocked_bot_is_not_asked_again(bot, store):
    await queue(store)
    method = SendMessage(chat_id=OWNER, text="x")
    bot.session.fail_once = TelegramForbiddenError(method, "Forbidden: bot was blocked by the user")
    notifier = TelegramMonitoringNotifier(bot, store)
    assert await notifier.deliver() == 1  # the summary still goes in the same round
    assert await store.pending(10) == ()
