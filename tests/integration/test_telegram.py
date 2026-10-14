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


@pytest.mark.parametrize(
    ("text", "fields", "kind"),
    [
        ("мой ИНН 7707083893, долг 100 000", {}, "text"),
        ("/start", {}, "command"),
        (
            None,
            {"document": {"file_id": "x", "file_unique_id": "x", "file_name": "dolgi.xlsx"}},
            "file",
        ),
    ],
)
async def test_a_refused_user_is_logged_by_id_without_what_they_sent(
    bot, update_factory, caplog, text, fields, kind
):
    """Who tried to get in is in the journal; what they wrote, their name and file are not."""
    update = update_factory(text, user_id=99, **fields)
    await create_dispatcher(frozenset({42})).feed_update(bot, update)
    assert bot.session.calls[-1].text == texts.access_denied(99)
    assert f"access_denied user_id=99 kind={kind}" in caplog.text
    for private in ("7707083893", "долг", "dolgi", "Test"):
        assert private not in caplog.text


async def test_an_allowed_user_leaves_no_access_record(bot, update_factory, caplog):
    await create_dispatcher(frozenset({42})).feed_update(bot, update_factory("/start"))
    assert "access_denied" not in caplog.text
