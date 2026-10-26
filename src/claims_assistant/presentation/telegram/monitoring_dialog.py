"""S8-09 (part A): the «Мониторинг» button — the list, adding, taking off, the file.

Its own router, included before the shell's: a file sent in the monitoring dialog is a
list, not «files are accepted only inside a check». The main menu's buttons and commands
are not this dialog's — they fall through to the shell, which leaves any dialog, so the
dialog never swallows «Статус» as a list of INNs.

What the user reads after adding or taking off is part B's (``watchlist.format_added`` and
``format_removed``); the screen, the prompts and the buttons are here.
"""

import logging
import re
from dataclasses import replace
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.imports import WorkbookError
from claims_assistant.application.monitoring import Frequency, MonitoringService
from claims_assistant.application.watchlist_files import read_watch_file
from claims_assistant.domain.watchlist import Candidate, parse_inns
from claims_assistant.infrastructure.excel.watchlist import build_watchlist

from . import texts
from .menu import (
    BACK,
    CANCEL,
    CHECK_INN,
    FREQUENCY_DAILY,
    FREQUENCY_WEEKDAYS,
    FREQUENCY_WEEKLY,
    MONITORING,
    NEW_CHECK,
    REPORT,
    STATUS,
    UNWATCH_PREFIX,
    WATCH_ADD,
    WATCH_CLEAR,
    WATCH_CLEAR_CONFIRM,
    WATCH_FILE,
    WATCH_FREQUENCY,
    WATCH_PAUSE,
    WATCH_REMOVE,
    WATCH_RESUME,
    cancel_menu,
    clear_menu,
    frequency_menu,
    main_menu,
    monitoring_menu,
)
from .watchlist import format_added, format_removed

logger = logging.getLogger(__name__)

_SPREADSHEET = (".xlsx", ".xls")
# The shell's buttons: they leave the monitoring dialog for the shell to answer.
_SHELL = frozenset({CHECK_INN, NEW_CHECK, STATUS, REPORT, "О сервисе", "Помощь"})


# A name written beside an INN on its own line («1. 7707083893 ПАО Сбербанк»): the INN,
# the numbering, dates and separators are taken out, what has a letter is the name. The
# customer typed names that way (17.10.2026) and saw an empty «Название» — the list kept
# INNs only until the first check brought the source's name.
_INN = re.compile(r"(?<!\d)(?:\d{10}|\d{12})(?!\d)")
_NUMBERING = re.compile(r"^\s*\d{1,3}[.)]\s+")
_DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_LABEL = re.compile(r"(?i)\bинн\b\s*[:№]?")
_EDGES = " \t,;:—–-|"


def names_in(text: str) -> dict[str, str]:
    """INN → the name on its line, for lines with exactly one INN and some letters."""
    names: dict[str, str] = {}
    for line in text.splitlines():
        found = _INN.findall(line)
        if len(found) != 1:
            continue
        rest = _INN.sub(" ", _NUMBERING.sub("", line))
        rest = _LABEL.sub(" ", _DATE.sub(" ", rest))
        name = " ".join(rest.split()).strip(_EDGES)
        if any(char.isalpha() for char in name):
            names.setdefault(found[0], name[:300])
    return names


def _with_names(candidates, text: str) -> list[Candidate]:
    names = names_in(text)
    return [
        replace(candidate, name=names[candidate.inn])
        if candidate.name is None and candidate.inn in names
        else candidate
        for candidate in candidates
    ]


class MonitorDialog(StatesGroup):
    menu = State()
    adding = State()
    removing = State()
    clearing = State()
    frequency = State()


_FREQUENCIES = {
    FREQUENCY_DAILY: Frequency.DAILY,
    FREQUENCY_WEEKDAYS: Frequency.WEEKDAYS,
    FREQUENCY_WEEKLY: Frequency.WEEKLY,
}


def _ours(message: Message) -> bool:
    """Text this dialog reads: not a command and not a button of the main menu."""
    text = message.text
    return bool(text) and not text.startswith("/") and text not in _SHELL


def monitoring_router(service: MonitoringService | None, reader) -> Router:
    router = Router(name="monitoring")
    steps = (
        MonitorDialog.adding,
        MonitorDialog.removing,
        MonitorDialog.clearing,
        MonitorDialog.frequency,
    )

    def schedule(frequency: Frequency = Frequency.DAILY) -> str:
        policy = service.policy
        return texts.watch_schedule(policy.run_at, policy.utc_offset_hours, frequency)

    async def keyboard(user_id: int):
        """The monitoring keyboard with «Приостановить» or «Возобновить», as it stands.

        A base that cannot be read gives the plain one: the reply «не удалось» goes out
        through here and must not fail on the same base.
        """
        try:
            settings = await service.settings(user_id)
        except RepositoryError as exc:
            logger.error("watch_settings_unread error_type=%s", type(exc).__name__)
            return monitoring_menu()
        return monitoring_menu(settings.paused)

    async def overview(message: Message, state: FSMContext) -> None:
        if service is None:
            await state.clear()
            await message.answer(texts.WATCH_UNAVAILABLE, reply_markup=main_menu())
            return
        user_id = message.from_user.id
        entries = await service.entries(user_id)
        settings = await service.settings(user_id)
        next_run = await service.next_run(user_id)
        status = texts.watch_status(settings, next_run, service.policy.run_at, service.local_now())
        await state.set_state(MonitorDialog.menu)
        await message.answer(
            texts.watch_overview(
                entries, service.policy.limit, schedule(settings.frequency), status
            ),
            reply_markup=monitoring_menu(settings.paused),
        )

    async def back(message: Message, state: FSMContext, text: str) -> None:
        await state.set_state(MonitorDialog.menu)
        await message.answer(text, reply_markup=await keyboard(message.from_user.id))

    async def added(message: Message, state: FSMContext, candidates, **summary) -> None:
        user_id = message.from_user.id
        try:
            names = {entry.inn: entry.name for entry in await service.entries(user_id)}
            result = await service.add(user_id, candidates)
        except RepositoryError as exc:
            logger.error("watch_add_failed error_type=%s", type(exc).__name__)
            await back(message, state, texts.WATCH_FAILED)
            return
        reply = format_added(result, **summary)
        renamed = [
            (c.inn, names.get(c.inn), c.name)
            for c in result.already
            if c.name and names.get(c.inn) != c.name
        ]
        if renamed:
            reply += "\n" + texts.watch_renamed(renamed)
        if result.added:
            frequency = (await service.settings(message.from_user.id)).frequency
            reply += "\n\n" + texts.watch_added_footer(schedule(frequency))
        await back(message, state, reply)

    async def not_saved(message: Message, state: FSMContext, exc: RepositoryError) -> None:
        logger.error("watch_settings_failed error_type=%s", type(exc).__name__)
        await back(message, state, texts.WATCH_SETTINGS_FAILED)

    # --- entry and exit ---

    @router.message(Command("monitor"))
    @router.message(F.text == MONITORING)
    async def open_monitoring(message: Message, state: FSMContext) -> None:
        await state.clear()
        await overview(message, state)

    @router.message(StateFilter(MonitorDialog), F.text == BACK)
    @router.message(MonitorDialog.menu, Command("cancel"))
    @router.message(MonitorDialog.menu, F.text == CANCEL)
    async def leave(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.BACK_TO_MENU, reply_markup=main_menu())

    @router.message(StateFilter(*steps), Command("cancel"))
    @router.message(StateFilter(*steps), F.text == CANCEL)
    async def cancel_step(message: Message, state: FSMContext) -> None:
        await back(message, state, texts.WATCH_STEP_CANCELLED)

    # --- the menu ---

    @router.message(MonitorDialog.menu, F.text == WATCH_ADD)
    async def ask_add(message: Message, state: FSMContext) -> None:
        await state.set_state(MonitorDialog.adding)
        await message.answer(texts.WATCH_ADD_PROMPT, reply_markup=cancel_menu())

    @router.message(MonitorDialog.menu, F.text == WATCH_REMOVE)
    async def ask_remove(message: Message, state: FSMContext) -> None:
        if not await service.entries(message.from_user.id):
            await message.answer(
                texts.WATCH_LIST_EMPTY, reply_markup=await keyboard(message.from_user.id)
            )
            return
        await state.set_state(MonitorDialog.removing)
        await message.answer(texts.WATCH_REMOVE_PROMPT, reply_markup=cancel_menu())

    @router.message(MonitorDialog.menu, F.text == WATCH_FILE)
    async def send_file(message: Message) -> None:
        entries = await service.entries(message.from_user.id)
        if not entries:
            await message.answer(
                texts.WATCH_LIST_EMPTY, reply_markup=await keyboard(message.from_user.id)
            )
            return
        day = service.today()
        data = build_watchlist(entries, day, service.policy.limit)
        await message.answer_document(
            BufferedInputFile(data, filename=f"monitoring-{day.isoformat()}.xlsx"),
            caption=texts.watch_file_caption(len(entries), service.policy.limit),
            reply_markup=await keyboard(message.from_user.id),
        )

    @router.message(MonitorDialog.menu, F.text == WATCH_CLEAR)
    async def ask_clear(message: Message, state: FSMContext) -> None:
        entries = await service.entries(message.from_user.id)
        if not entries:
            await message.answer(
                texts.WATCH_CLEAR_EMPTY, reply_markup=await keyboard(message.from_user.id)
            )
            return
        await state.set_state(MonitorDialog.clearing)
        await message.answer(texts.watch_clear_prompt(len(entries)), reply_markup=clear_menu())

    # --- pause and frequency (S8-12) ---

    @router.message(MonitorDialog.menu, F.text == WATCH_PAUSE)
    async def pause(message: Message, state: FSMContext) -> None:
        try:
            settings = await service.pause(message.from_user.id)
        except RepositoryError as exc:
            await not_saved(message, state, exc)
            return
        await back(message, state, texts.watch_paused(settings.paused_since))

    @router.message(MonitorDialog.menu, F.text == WATCH_RESUME)
    async def resume(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id
        try:
            await service.resume(user_id)
            next_run = await service.next_run(user_id)
        except RepositoryError as exc:
            await not_saved(message, state, exc)
            return
        resumed = texts.watch_resumed(next_run, service.policy.run_at, service.local_now())
        await back(message, state, resumed)

    @router.message(MonitorDialog.menu, F.text == WATCH_FREQUENCY)
    async def ask_frequency(message: Message, state: FSMContext) -> None:
        await state.set_state(MonitorDialog.frequency)
        await message.answer(await frequency_prompt(message), reply_markup=frequency_menu())

    @router.message(MonitorDialog.frequency, F.text.in_(_FREQUENCIES))
    async def set_frequency(message: Message, state: FSMContext) -> None:
        frequency = _FREQUENCIES[message.text]
        try:
            await service.set_frequency(message.from_user.id, frequency)
        except RepositoryError as exc:
            await not_saved(message, state, exc)
            return
        await back(message, state, texts.watch_frequency_set(schedule(frequency)))

    @router.message(MonitorDialog.frequency, _ours)
    async def remind_frequency(message: Message) -> None:
        await message.answer(await frequency_prompt(message), reply_markup=frequency_menu())

    async def frequency_prompt(message: Message) -> str:
        """The price of each choice for this owner's list as it is now (S8-12, part B)."""
        return texts.watch_frequency_prompt(len(await service.entries(message.from_user.id)))

    @router.message(MonitorDialog.clearing, F.text == WATCH_CLEAR_CONFIRM)
    async def clear(message: Message, state: FSMContext) -> None:
        try:
            count = await service.clear(message.from_user.id)
        except RepositoryError as exc:
            logger.error("watch_clear_failed error_type=%s", type(exc).__name__)
            await back(message, state, texts.WATCH_FAILED)
            return
        await back(message, state, texts.watch_cleared(count))

    @router.message(MonitorDialog.clearing, _ours)
    async def remind_clear(message: Message) -> None:
        await message.answer(texts.WATCH_CLEAR_HINT, reply_markup=clear_menu())

    # --- adding and taking off ---

    @router.message(MonitorDialog.adding, _ours)
    async def add_from_text(message: Message, state: FSMContext) -> None:
        parsed = parse_inns(message.text)
        if not parsed.candidates and not parsed.invalid:
            await message.answer(texts.WATCH_NO_INN, reply_markup=cancel_menu())
            return
        candidates = _with_names(parsed.candidates, message.text)
        await added(message, state, candidates, invalid=parsed.invalid, repeats=parsed.repeats)

    @router.message(StateFilter(MonitorDialog.menu, MonitorDialog.adding), F.document)
    async def add_from_file(message: Message, state: FSMContext, bot: Bot) -> None:
        document = message.document
        if not (document.file_name or "").lower().endswith(_SPREADSHEET):
            await message.answer(texts.WATCH_FILE_TYPE, reply_markup=cancel_menu())
            return
        if document.file_size is None or document.file_size > texts.MAX_UPLOAD_BYTES:
            await message.answer(texts.FILE_TOO_LARGE, reply_markup=cancel_menu())
            return
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        try:
            listed = read_watch_file(
                buffer.getvalue(), reader, overdue_days=service.policy.overdue_days
            )
        except WorkbookError as error:
            await message.answer(
                f"{error.reason} {texts.WATCH_FILE_UNREADABLE}", reply_markup=cancel_menu()
            )
            return
        if listed.problem is not None:
            await message.answer(listed.problem, reply_markup=cancel_menu())
            return
        await added(
            message,
            state,
            listed.candidates,
            invalid=listed.invalid,
            repeats=listed.repeats,
            without_inn=listed.without_inn,
            from_file=True,
            cut=listed.cut,
        )

    @router.message(MonitorDialog.removing, _ours)
    async def remove_from_text(message: Message, state: FSMContext) -> None:
        parsed = parse_inns(message.text)
        if not parsed.candidates and not parsed.invalid:
            await message.answer(texts.WATCH_NO_INN, reply_markup=cancel_menu())
            return
        try:
            result = await service.remove(
                message.from_user.id, [candidate.inn for candidate in parsed.candidates]
            )
        except RepositoryError as exc:
            logger.error("watch_remove_failed error_type=%s", type(exc).__name__)
            await back(message, state, texts.WATCH_FAILED)
            return
        await back(message, state, format_removed(result, parsed.invalid, service.policy.limit))

    @router.message(
        StateFilter(MonitorDialog.removing, MonitorDialog.clearing, MonitorDialog.frequency),
        F.document,
    )
    async def file_out_of_place(message: Message) -> None:
        await message.answer(texts.WATCH_FILE_NOT_HERE, reply_markup=cancel_menu())

    @router.message(MonitorDialog.menu, _ours)
    async def menu_hint(message: Message) -> None:
        await message.answer(
            texts.WATCH_MENU_HINT, reply_markup=await keyboard(message.from_user.id)
        )

    # --- the button under a change notice ---

    @router.callback_query(F.data.startswith(UNWATCH_PREFIX))
    async def unwatch(query: CallbackQuery) -> None:
        inn = query.data.removeprefix(UNWATCH_PREFIX)
        if service is None:
            await query.answer(texts.WATCH_UNAVAILABLE)
            return
        result = await service.remove(query.from_user.id, [inn])
        notice = query.message if isinstance(query.message, Message) else None
        if not result.removed:
            await query.answer(texts.UNWATCH_ABSENT)
        else:
            await query.answer(texts.UNWATCHED_SHORT)
            if notice is not None:
                await notice.answer(texts.unwatched(inn, result.total, service.policy.limit))
        if notice is not None:
            # One tap is enough: the button goes, the notice stays.
            await notice.edit_reply_markup(reply_markup=None)

    return router
