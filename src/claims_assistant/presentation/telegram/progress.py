"""S8-03: what the user sees while the bot waits for the source.

One INN costs several requests to the source; on a bad link that is tens of seconds of
silence, and the customer asked to see at once that the bot took the INN (15.10.2026). A
short note goes before the first request and is deleted once the answer is sent, and
«печатает» stays on meanwhile. Neither may break the check: if Telegram fails to show them,
only the hint is lost, so their errors are logged by type (or not at all) and swallowed.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import Message

logger = logging.getLogger(__name__)

# Telegram shows «печатает» for five seconds or until the bot's next message: renewed sooner.
TYPING_EVERY_SECONDS = 4.0


@asynccontextmanager
async def note(message: Message, text: str) -> AsyncIterator[None]:
    """Send ``text`` in reply now and delete it on the way out, whatever happened inside."""
    sent: Message | None = None
    try:
        sent = await message.answer(text)
    except Exception as exc:
        logger.warning("progress_note_not_sent error_type=%s", type(exc).__name__)
    try:
        yield
    finally:
        if sent is not None:
            try:
                await message.bot.delete_message(chat_id=sent.chat.id, message_id=sent.message_id)
            except Exception as exc:
                # Left in the chat it says nothing wrong; the answer below it is what counts.
                logger.warning("progress_note_not_deleted error_type=%s", type(exc).__name__)


@asynccontextmanager
async def typing(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """Keep «печатает» on in the chat until the block ends.

    End the block before sending the answer: a renewal sent after it would show «печатает»
    under a message that is already there.
    """

    async def renew() -> None:
        while True:
            # Not logged: on a bad link this would be a line every few seconds.
            with suppress(Exception):
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(TYPING_EVERY_SECONDS)

    task = asyncio.create_task(renew())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
