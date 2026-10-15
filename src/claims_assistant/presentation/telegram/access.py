import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject

from .texts import access_denied

logger = logging.getLogger(__name__)


def _kind(message: Message) -> str:
    """What was sent, in one word: the content itself never goes to the journal."""
    if message.text:
        return "command" if message.text.startswith("/") else "text"
    if message.document:
        return "file"
    return "other"


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self.allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type != ChatType.PRIVATE:
            return None
        if event.from_user is None:
            return None
        if event.from_user.id not in self.allowed_ids:
            # Who tried to get in, for the operator; the ID is what the reply shows them too.
            logger.warning("access_denied user_id=%s kind=%s", event.from_user.id, _kind(event))
            await event.answer(access_denied(event.from_user.id))
            return None
        return await handler(event, data)
