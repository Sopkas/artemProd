import logging

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, Message

from . import texts
from .access import AccessMiddleware
from .menu import main_menu

logger = logging.getLogger(__name__)


def create_dispatcher(allowed_ids: frozenset[int]) -> Dispatcher:
    dispatcher = Dispatcher(disable_fsm=True)
    router = Router(name="shell")
    router.message.outer_middleware(AccessMiddleware(allowed_ids))

    @router.message(F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.video_note)
    async def attachment(message: Message) -> None:
        await message.answer(texts.FILE_UNSUPPORTED, reply_markup=main_menu())

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(texts.START, reply_markup=main_menu())

    @router.message(Command("help"))
    @router.message(F.text == "Помощь")
    async def help_command(message: Message) -> None:
        await message.answer(texts.HELP, reply_markup=main_menu())

    @router.message(Command("about"))
    @router.message(F.text == "О сервисе")
    async def about(message: Message) -> None:
        await message.answer(texts.ABOUT, reply_markup=main_menu())

    @router.message()
    async def fallback(message: Message) -> None:
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
