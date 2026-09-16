import pytest
from aiogram.methods import SendMessage

from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.handlers import create_dispatcher

# Synthetic value built for the checksum, not a real organization.
VALID_INN = "1234567894"


def buttons(reply: SendMessage) -> list[str]:
    return [button.text for row in reply.reply_markup.keyboard for button in row]


async def feed(dispatcher, bot, update_factory, *messages: str, user_id: int = 42) -> SendMessage:
    for text in messages:
        await dispatcher.feed_update(bot, update_factory(text, user_id=user_id))
    return bot.session.calls[-1]


@pytest.mark.parametrize("entry", ["Проверить ИНН", "/inn"])
async def test_entry_prompts_for_inn_with_cancel_keyboard(bot, update_factory, entry):
    reply = await feed(create_dispatcher(frozenset({42})), bot, update_factory, entry)
    assert reply.text == texts.INN_PROMPT
    assert buttons(reply) == ["Отмена"]


async def test_main_menu_has_check_inn_button(bot, update_factory):
    reply = await feed(create_dispatcher(frozenset({42})), bot, update_factory, "/start")
    assert buttons(reply) == ["Проверить ИНН", "О сервисе", "Помощь"]


async def test_valid_inn_is_accepted_and_dialog_ends(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", f" {VALID_INN} ")
    assert reply.text == texts.inn_accepted(VALID_INN)
    assert buttons(reply) == ["Проверить ИНН", "О сервисе", "Помощь"]
    # The dialog is over: plain text goes back to the fallback, not to the validator.
    reply = await feed(dispatcher, bot, update_factory, "привет")
    assert reply.text == texts.FALLBACK


@pytest.mark.parametrize(
    "raw",
    ["12345", "abc", "123456789012", "1234567895"],
)
async def test_invalid_inn_explains_and_keeps_waiting(bot, update_factory, raw):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", raw)
    assert reply.text.endswith(texts.INN_RETRY)
    assert "ИНН" in reply.text
    assert raw not in reply.text
    assert buttons(reply) == ["Отмена"]
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.inn_accepted(VALID_INN)


@pytest.mark.parametrize("cancel", ["Отмена", "/cancel"])
async def test_cancel_returns_to_menu(bot, update_factory, cancel):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", cancel)
    assert reply.text == texts.INN_CANCELLED
    assert buttons(reply) == ["Проверить ИНН", "О сервисе", "Помощь"]
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.FALLBACK


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/start", texts.START),
        ("/help", texts.HELP),
        ("Помощь", texts.HELP),
        ("/about", texts.ABOUT),
    ],
)
async def test_menu_commands_leave_the_dialog(bot, update_factory, command, expected):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", command)
    assert reply.text == expected
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.FALLBACK


async def test_cancel_outside_dialog_is_harmless(bot, update_factory):
    reply = await feed(create_dispatcher(frozenset({42})), bot, update_factory, "/cancel")
    assert reply.text == texts.NOTHING_TO_CANCEL
    assert buttons(reply) == ["Проверить ИНН", "О сервисе", "Помощь"]


async def test_dialog_state_is_per_user(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42, 43}))
    await feed(dispatcher, bot, update_factory, "Проверить ИНН", user_id=42)
    reply = await feed(dispatcher, bot, update_factory, VALID_INN, user_id=43)
    assert reply.text == texts.FALLBACK


@pytest.mark.parametrize("text", ["Проверить ИНН", "/inn", VALID_INN, "Отмена"])
async def test_unlisted_user_cannot_use_the_dialog(bot, update_factory, text):
    reply = await feed(create_dispatcher(frozenset({42})), bot, update_factory, text, user_id=99)
    assert reply.text == texts.access_denied(99)
    assert reply.reply_markup is None
