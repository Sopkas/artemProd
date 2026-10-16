import asyncio

import pytest
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import DeleteMessage, SendChatAction, SendMessage

from claims_assistant.application.card_review import CardReview
from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import Section
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.presentation.telegram import texts
from claims_assistant.presentation.telegram.card import DEMO_BANNER, REVIEW_MONTH, REVIEW_TITLE
from claims_assistant.presentation.telegram.handlers import create_dispatcher

# Synthetic value built for the checksum, not a real organization.
VALID_INN = "1234567894"


MAIN_MENU = ["Проверить ИНН", "Новая проверка", "Статус", "Отчёт", "О сервисе", "Помощь"]


def buttons(reply: SendMessage) -> list[str]:
    return [button.text for row in reply.reply_markup.keyboard for button in row]


async def feed(dispatcher, bot, update_factory, *messages: str, user_id: int = 42) -> SendMessage:
    for text in messages:
        await dispatcher.feed_update(bot, update_factory(text, user_id=user_id))
    return bot.session.last_message


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
    # The brief card: the conclusion with its mark right under the demo banner.
    assert reply.text.startswith(DEMO_BANNER)
    assert f"ИНН {VALID_INN}" in reply.text
    assert reply.text.splitlines()[2].startswith("ℹ️ ")  # no data of the user's: not ✅
    assert "Источник:" not in reply.text and len(reply.text) < 500
    assert buttons(reply) == ["Подробнее", *MAIN_MENU]
    # «Подробнее» sends the full card for the same INN and gives the main menu back.
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert reply.text.startswith(DEMO_BANNER)
    assert f"ИНН {VALID_INN}" in reply.text
    assert "Организация — проверено полностью" in reply.text
    assert reply.text.count("Источник:") == len(Section)
    assert buttons(reply) == MAIN_MENU
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
    # Nothing was read: the brief card says so and names the source's one reason.
    assert "❔ Внешние данные неполные" in reply.text
    assert "Причина: Синтетический пример сбоя источника." in reply.text
    assert "✅" not in reply.text
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert reply.text.count("— раздел недоступен") == len(Section)
    assert buttons(reply) == MAIN_MENU


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
    # The register's message is the reason of the first line; its text is in the details.
    assert "⚠️ Есть сообщения ЕФРСБ — роль организации не подтверждена" in reply.text
    assert "ЕФРСБ" in reply.text
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
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
    assert "Долг 10,00 ₽" in reply.text
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert "Внутренние данные — проверка от 01.09.2026" in reply.text
    assert "Долг: 10,00 ₽" in reply.text
    # Another allowed user gets the plain card: the package is not theirs.
    reply = await feed(dispatcher, bot, update_factory, "Проверить ИНН", VALID_INN, user_id=43)
    assert "Внутренние данные" not in reply.text
    assert "Своих данных по этой организации нет" in reply.text
    # …and «Подробнее» for them is their own card of the INN, without the owner's block.
    reply = await feed(dispatcher, bot, update_factory, "Подробнее", user_id=43)
    assert f"ИНН {VALID_INN}" in reply.text and "Внутренние данные" not in reply.text


async def test_details_without_a_card_asks_to_check_an_inn_first(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert reply.text == texts.CARD_NO_DETAILS
    assert buttons(reply) == MAIN_MENU
    # The remembered INN does not outlive another action of the menu.
    await feed(dispatcher, bot, update_factory, "/inn", VALID_INN, "/start")
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert reply.text == texts.CARD_NO_DETAILS


class FailsThenAnswers:
    """The source fails the first time and answers in full afterwards. A failure is not
    cached, so a second request would show the user a card other than the brief one."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, request: CompanyDataRequest):
        self.calls += 1
        scenario = DemoScenario.ERROR if self.calls == 1 else DemoScenario.ORDINARY
        return await DemoCompanyDataProvider(scenario).fetch(request)


async def test_details_send_the_card_of_the_same_answer_without_asking_the_source(
    bot, update_factory
):
    """«Подробнее» is the rest of the card already shown: no new request to the source
    (3 of the day's 100 at Checko) and no other picture than the brief card's (#86 review)."""
    provider = FailsThenAnswers()
    dispatcher = create_dispatcher(frozenset({42}), provider)
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    assert "❔ Внешние данные неполные" in reply.text
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert reply.text.count("— раздел недоступен") == len(Section)
    assert provider.calls == 1


async def test_details_typed_inside_another_dialog_leave_that_dialog_alone(bot, update_factory):
    """The word is a button of the brief card only; typed at the date step it is a bad date,
    and the new check goes on (#86 review)."""
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN, "Новая проверка")
    assert reply.text == texts.CHECK_DATE_PROMPT
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert reply.text == texts.CHECK_DATE_INVALID
    reply = await feed(dispatcher, bot, update_factory, "01.10.2026")
    assert reply.text != texts.CHECK_DATE_INVALID


# --- S8-03: «💰 Считаю миллионы…» and «печатает» while the source is asked ---


class SlowProvider:
    """The demo source a little slower: what the user had seen when the source was asked."""

    def __init__(self, session, *, fail: bool = False) -> None:
        self.session = session
        self.fail = fail
        self.seen: list = []

    async def fetch(self, request: CompanyDataRequest):
        self.seen = list(self.session.calls)
        await asyncio.sleep(0.05)
        if self.fail:
            raise RuntimeError("private debtor note")
        return await DemoCompanyDataProvider().fetch(request)


def kinds(calls) -> list[str]:
    return [type(call).__name__ for call in calls]


async def test_counting_note_comes_before_the_source_and_goes_with_the_card(bot, update_factory):
    provider = SlowProvider(bot.session)
    dispatcher = create_dispatcher(frozenset({42}), provider)
    await feed(dispatcher, bot, update_factory, "/inn")
    before = len(bot.session.calls)
    await feed(dispatcher, bot, update_factory, VALID_INN)
    # The note is already in the chat when the source is asked.
    assert provider.seen[-1].text == texts.COUNTING
    calls = bot.session.calls[before:]
    # «печатает» while waiting and not after the card: it would hang under the answer.
    assert kinds(calls) == ["SendMessage", "SendChatAction", "SendMessage", "DeleteMessage"]
    assert calls[1].action == ChatAction.TYPING and calls[1].chat_id == 42
    assert f"ИНН {VALID_INN}" in calls[2].text
    assert buttons(calls[2]) == ["Подробнее", *MAIN_MENU]
    assert (calls[3].chat_id, calls[3].message_id) == (42, 100)


async def test_counting_note_does_not_stay_when_the_source_fails(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}), SlowProvider(bot.session, fail=True))
    await feed(dispatcher, bot, update_factory, "/inn")
    before = len(bot.session.calls)
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert reply.text == texts.CHECK_FAILED
    calls = bot.session.calls[before:]
    assert kinds(calls) == ["SendMessage", "SendChatAction", "SendMessage", "DeleteMessage"]
    assert calls[0].text == texts.COUNTING


async def test_mistyped_inn_gets_no_counting_note(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    await feed(dispatcher, bot, update_factory, "/inn")
    before = len(bot.session.calls)
    reply = await feed(dispatcher, bot, update_factory, "12345")
    assert reply.text.endswith(texts.INN_RETRY)
    assert kinds(bot.session.calls[before:]) == ["SendMessage"]


async def test_card_comes_even_if_telegram_drops_the_note(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    await feed(dispatcher, bot, update_factory, "/inn")
    before = len(bot.session.calls)
    # The next request to Telegram is the note itself.
    bot.session.fail_once = TelegramNetworkError(SendMessage(chat_id=42, text="x"), "down")
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    assert f"ИНН {VALID_INN}" in reply.text
    assert "DeleteMessage" not in kinds(bot.session.calls[before:])


async def test_card_stands_even_if_the_note_cannot_be_deleted(bot, update_factory, monkeypatch):
    original = bot.session.make_request

    async def make_request(bot_, method, timeout=None):
        if isinstance(method, DeleteMessage | SendChatAction):
            bot.session.calls.append(method)
            raise TelegramNetworkError(method, "down")
        return await original(bot_, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", make_request)
    dispatcher = create_dispatcher(frozenset({42}), SlowProvider(bot.session))
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    # No error reply after the card: a lost hint is not a failed check.
    assert f"ИНН {VALID_INN}" in reply.text
    assert reply.text != texts.ERROR
    assert isinstance(bot.session.calls[-1], DeleteMessage)


# --- S8-05: the model's reading under the card ---


class Reviewer:
    """The card reviewer as the dialog sees it: one call per card, or a failure."""

    def __init__(self, review=None, fail: bool = False) -> None:
        self.review_ = review
        self.fail = fail
        self.calls = []

    async def review(self, check, assessment, internal):
        self.calls.append((check.inn, assessment.inn, internal))
        if self.fail:
            raise RuntimeError("private debtor note")
        return self.review_


async def test_the_reading_comes_after_the_card_with_its_own_note(bot, update_factory):

    reviewer = Reviewer(CardReview(month_exhausted=True))
    dispatcher = create_dispatcher(frozenset({42}), card_reviewer=reviewer)
    await feed(dispatcher, bot, update_factory, "/inn")
    before = len(bot.session.calls)
    reply = await feed(dispatcher, bot, update_factory, VALID_INN)
    calls = [c for c in bot.session.calls[before:] if not isinstance(c, SendChatAction)]
    texts_sent = [c.text for c in calls if isinstance(c, SendMessage)]
    # Counting note, the card, the reading's note, the one line on why there is none.
    assert texts_sent[0] == texts.COUNTING
    assert f"ИНН {VALID_INN}" in texts_sent[1]
    assert texts_sent[2] == texts.REVIEWING
    assert REVIEW_MONTH in reply.text and REVIEW_TITLE not in reply.text
    assert buttons(reply) == ["Подробнее", *MAIN_MENU]
    assert kinds(calls)[-1] == "DeleteMessage"  # the reading's note does not stay
    assert reviewer.calls == [(VALID_INN, VALID_INN, None)]
    # «Подробнее» still answers with the full card of the same answer.
    reply = await feed(dispatcher, bot, update_factory, "Подробнее")
    assert "Организация — проверено полностью" in reply.text


async def test_a_failed_reading_leaves_the_card_and_says_so(bot, update_factory, caplog):
    dispatcher = create_dispatcher(frozenset({42}), card_reviewer=Reviewer(fail=True))
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    assert "Карточка выше — по правилам" in reply.text
    assert "private debtor note" not in caplog.text
    assert "card_review_failed error_type=RuntimeError" in caplog.text


async def test_without_a_reviewer_the_card_is_the_last_word(bot, update_factory):
    dispatcher = create_dispatcher(frozenset({42}))
    reply = await feed(dispatcher, bot, update_factory, "/inn", VALID_INN)
    assert f"ИНН {VALID_INN}" in reply.text
    assert texts.REVIEWING not in [c.text for c in bot.session.calls if isinstance(c, SendMessage)]
