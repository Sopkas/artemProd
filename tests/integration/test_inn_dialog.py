import pytest
from aiogram.methods import SendMessage

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.card import DEMO_BANNER
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
    assert buttons(reply) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]


async def test_valid_inn_shows_the_demo_card_and_dialog_ends(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", f" {VALID_INN} ")
    assert reply.text.startswith(DEMO_BANNER)
    assert f"ИНН {VALID_INN}" in reply.text
    assert "Организация" in reply.text
    assert buttons(reply) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]
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
    assert reply.text.startswith(DEMO_BANNER)


@pytest.mark.parametrize("cancel", ["Отмена", "/cancel"])
async def test_cancel_returns_to_menu(bot, update_factory, cancel):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", cancel)
    assert reply.text == texts.INN_CANCELLED
    assert buttons(reply) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]
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
    assert buttons(reply) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]


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


@pytest.mark.parametrize(
    "payload",
    [
        {"contact": {"phone_number": "000", "first_name": "Test"}},
        {"document": {"file_id": "test", "file_unique_id": "test"}, "caption": "/inn"},
    ],
)
async def test_non_text_returns_menu_and_clears_dialog(bot, update_factory, payload):
    dispatcher = create_dispatcher(frozenset({42}))
    await feed(dispatcher, bot, update_factory, "/inn")
    await dispatcher.feed_update(bot, update_factory(None, **payload))
    assert buttons(bot.session.calls[-1]) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.FALLBACK


async def test_group_cannot_enter_private_dialog(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    await dispatcher.feed_update(bot, update_factory("/inn", chat_type="group"))
    assert not bot.session.calls
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.FALLBACK


async def test_configured_scenario_is_used_for_the_card(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}), DemoCompanyDataProvider(DemoScenario.ERROR))
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    assert reply.text.count("— раздел недоступен") == 3
    assert buttons(reply) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]


async def test_provider_bug_gives_a_safe_reply_and_leaves_the_dialog(bot, update_factory, caplog):
    class BrokenProvider:
        async def fetch(self, request: CompanyDataRequest):
            raise RuntimeError("private debtor note")

    dispatcher = create_dispatcher(frozenset({42}), BrokenProvider())
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    assert reply.text == texts.CHECK_FAILED
    assert buttons(reply) == [
        "Проверить ИНН",
        "Новая проверка",
        "Статус",
        "Отчёт",
        "О сервисе",
        "Помощь",
    ]
    assert "check_failed" in caplog.text
    assert "private debtor note" not in caplog.text
    assert VALID_INN not in caplog.text
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.FALLBACK


async def test_card_works_through_the_guarded_provider(bot, update_factory):
    from claims_assistant.application.external_guard import GuardedCompanyDataProvider, GuardPolicy
    from claims_assistant.infrastructure.cache.memory import TtlSnapshotCache

    provider = GuardedCompanyDataProvider(
        DemoCompanyDataProvider(DemoScenario.ALARM), GuardPolicy(), TtlSnapshotCache(60)
    )
    dispatcher = create_dispatcher(frozenset({42}), provider)
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    assert reply.text.startswith(DEMO_BANNER)
    assert "наблюдени" in reply.text


async def test_card_includes_the_owners_data_after_a_finished_check(bot, update_factory, tmp_path):
    """S4-04: the INN card shows what the owner's own package says about the company."""
    from datetime import date
    from decimal import Decimal

    from claims_assistant.application.analysis_queue import RunOutcome
    from claims_assistant.application.check_package import accept_counterparties
    from claims_assistant.domain.analysis import RunStatus
    from claims_assistant.domain.counterparties import CounterpartyRow
    from claims_assistant.domain.external import DataMode
    from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
    from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
    from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
    from claims_assistant.infrastructure.storage.local import LocalFileStorage

    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    reader = OpenpyxlSheetReader()
    rows = (CounterpartyRow(inn=VALID_INN, cutoff_date=date(2026, 9, 1), debt=Decimal("10.00")),)
    result = await accept_counterparties(
        42,
        date(2026, 9, 1),
        DataMode.DEMO,
        build_counterparties_template(rows),
        repository=repository,
        files=files,
        reader=reader,
    )
    await repository.transition(42, result.run.id, RunStatus.QUEUED)
    await repository.claim_next()
    await repository.finish(result.run.id, RunOutcome(RunStatus.COMPLETED))

    dispatcher = create_dispatcher(
        frozenset({42, 43}), repository=repository, files=files, reader=reader
    )
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", VALID_INN)
    assert "Внутренние данные — проверка от 01.09.2026" in reply.text
    assert "Долг: 10,00 ₽" in reply.text
    # Another allowed user gets the plain card: the package is not theirs.
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", VALID_INN, user_id=43)
    assert "Внутренние данные" not in reply.text
