import pytest
from aiogram.methods import SendMessage

from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.handlers import create_dispatcher


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("/start", texts.START),
        ("/help", texts.HELP),
        ("Помощь", texts.HELP),
        ("/about", texts.ABOUT),
        ("О сервисе", texts.ABOUT),
        ("/unknown", texts.FALLBACK),
        ("привет", texts.FALLBACK),
        ("7700000000", texts.FALLBACK),
    ],
)
async def test_commands_and_menu(bot, update_factory, input_text, expected):
    dispatcher = create_dispatcher(frozenset({42}))
    await dispatcher.feed_update(bot, update_factory(input_text))
    assert len(bot.session.calls) == 1
    reply = bot.session.calls[0]
    assert isinstance(reply, SendMessage)
    assert reply.text == expected
    assert [button.text for row in reply.reply_markup.keyboard for button in row] == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]


@pytest.mark.parametrize("text", ["/start", "/help", "Помощь", "/about", "О сервисе", "hello"])
async def test_unlisted_user_only_gets_denial(bot, update_factory, text):
    await create_dispatcher(frozenset({42})).feed_update(bot, update_factory(text, user_id=99))
    assert len(bot.session.calls) == 1
    reply = bot.session.calls[0]
    assert reply.text == texts.access_denied(99)
    assert reply.reply_markup is None


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
@pytest.mark.parametrize("user_id", [42, 99])
async def test_non_private_messages_ignored(bot, update_factory, chat_type, user_id):
    await create_dispatcher(frozenset({42})).feed_update(
        bot, update_factory(chat_type=chat_type, user_id=user_id)
    )
    assert bot.session.calls == []


@pytest.mark.parametrize("user_id", [42, 99])
@pytest.mark.parametrize("caption", [None, "/start", "/help"])
async def test_files_are_not_downloaded(bot, update_factory, user_id, caption):
    update = update_factory(
        None,
        user_id=user_id,
        caption=caption,
        document={"file_id": "demo", "file_unique_id": "demo", "file_name": "debt.xlsx"},
    )
    await create_dispatcher(frozenset({42})).feed_update(bot, update)
    assert len(bot.session.calls) == 1
    assert isinstance(bot.session.calls[0], SendMessage)
    expected = texts.FILE_UNSUPPORTED if user_id == 42 else texts.access_denied(user_id)
    assert bot.session.calls[0].text == expected


async def test_handler_error_is_contained_without_logging_private_data(bot, update_factory, caplog):
    dispatcher = create_dispatcher(frozenset({42}))
    bot.session.fail_once = RuntimeError("private payment comment and secret token")
    await dispatcher.feed_update(bot, update_factory("private message"))
    assert bot.session.calls[-1].text == texts.ERROR
    assert "handler_failed" in caplog.text
    assert "private" not in caplog.text
    assert "secret token" not in caplog.text
    await dispatcher.feed_update(bot, update_factory("/help"))
    assert bot.session.calls[-1].text == texts.HELP
