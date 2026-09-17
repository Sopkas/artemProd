import logging

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ErrorEvent, Message

from claims_assistant.application.check_company import check_company
from claims_assistant.application.company_data import CompanyDataProvider
from claims_assistant.domain.inn import InvalidInn
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider

from . import texts
from .access import AccessMiddleware
from .card import format_card
from .menu import CANCEL, CHECK_INN, cancel_menu, main_menu

logger = logging.getLogger(__name__)


class InnDialog(StatesGroup):
    waiting_for_inn = State()


def create_dispatcher(
    allowed_ids: frozenset[int], provider: CompanyDataProvider | None = None
) -> Dispatcher:
    if provider is None:
        provider = DemoCompanyDataProvider()
    # Dialog state lives in memory: it is short and may be lost on restart (S0-03, remark 2).
    dispatcher = Dispatcher()
    router = Router(name="shell")
    router.message.outer_middleware(AccessMiddleware(allowed_ids))

    @router.message(F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.video_note)
    async def attachment(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.FILE_UNSUPPORTED, reply_markup=main_menu())

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.START, reply_markup=main_menu())

    @router.message(Command("help"))
    @router.message(F.text == "Помощь")
    async def help_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.HELP, reply_markup=main_menu())

    @router.message(Command("about"))
    @router.message(F.text == "О сервисе")
    async def about(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.ABOUT, reply_markup=main_menu())

    @router.message(Command("inn"))
    @router.message(F.text == CHECK_INN)
    async def ask_inn(message: Message, state: FSMContext) -> None:
        await state.set_state(InnDialog.waiting_for_inn)
        await message.answer(texts.INN_PROMPT, reply_markup=cancel_menu())

    @router.message(InnDialog.waiting_for_inn, Command("cancel"))
    @router.message(InnDialog.waiting_for_inn, F.text == CANCEL)
    async def cancel_inn(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.INN_CANCELLED, reply_markup=main_menu())

    @router.message(InnDialog.waiting_for_inn, F.text)
    async def receive_inn(message: Message, state: FSMContext) -> None:
        try:
            check = await check_company(message.text or "", provider)
        except InvalidInn as error:
            await message.answer(texts.inn_invalid(str(error)), reply_markup=cancel_menu())
            return
        except Exception as exc:
            # The INN and the provider's message may be private; log only the error type.
            logger.error("check_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        await state.clear()
        await message.answer(format_card(check), reply_markup=main_menu())

    @router.message(StateFilter(None), Command("cancel"))
    async def nothing_to_cancel(message: Message) -> None:
        await message.answer(texts.NOTHING_TO_CANCEL, reply_markup=main_menu())

    @router.message()
    async def fallback(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.FALLBACK, reply_markup=main_menu())

    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> bool:
        # Never log exception text or the update: both can contain private data.
        logger.error(
            "handler_failed update_id=%s error_type=%s",
            event.update.update_id,
            type(event.exception).__name__,
        )
        message = event.update.message
        if (
            message is not None
            and message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_ids
        ):
            try:
                await message.answer(texts.ERROR, reply_markup=main_menu())
            except Exception as exc:
                logger.error("error_reply_failed error_type=%s", type(exc).__name__)
        return True

    dispatcher.include_router(router)
    return dispatcher
