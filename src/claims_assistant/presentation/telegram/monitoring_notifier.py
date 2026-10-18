"""S8-09 (part A): sending what the daily run left in the outbox.

Runs from the scheduler, outside any handler, so it sends by chat id: in a private chat
the chat id is the owner's Telegram id. A notice about one company carries the «Больше не
следить» button. Telegram away (11.10 and 13.10.2026 it was, for hours) leaves the rest
waiting for the next tick, in order; a refusal for good — the user blocked the bot — is
recorded and not retried.
"""

import logging
from collections.abc import Sequence
from datetime import date

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.monitoring import DayResult, NextCheck, WatchStore
from claims_assistant.domain.monitoring import Comparison

from . import texts
from .menu import unwatch_button
from .monitoring import Checked, format_changes, format_daily_summary

logger = logging.getLogger(__name__)


class TelegramMonitoringTexts:
    """``MonitoringTexts`` by part B's words (``presentation/telegram/monitoring``)."""

    def changes(self, inn: str, name: str | None, comparison: Comparison, day: date) -> str | None:
        return format_changes(inn, name, comparison, day)

    def summary(
        self,
        day: date,
        results: Sequence[DayResult],
        waiting: int = 0,
        next_check: NextCheck | None = None,
    ) -> str:
        after = [texts.watch_next_check(next_check, day)] if next_check else []
        if not results:
            return "\n\n".join([texts.watch_nothing_reached(day, waiting), *after])
        checked = [Checked(result.inn, result.name, result.comparison) for result in results]
        notes = [texts.watch_waiting(waiting)] if waiting else []
        return format_daily_summary(day, checked, [*notes, *after])


class TelegramMonitoringNotifier:
    def __init__(self, bot: Bot, store: WatchStore, batch: int = 50) -> None:
        self._bot = bot
        self._store = store
        self._batch = batch

    async def deliver(self) -> int:
        """Send what waits, oldest first; returns how many went out."""
        sent = 0
        for message in await self._store.pending(self._batch):
            markup = unwatch_button(message.inn) if message.inn else None
            try:
                await self._bot.send_message(message.owner_id, message.text, reply_markup=markup)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                # Blocked, or a text Telegram will never take: retrying cannot help.
                logger.error("monitoring_message_refused error_type=%s", type(exc).__name__)
                await self._store.mark_failed(message.id, type(exc).__name__)
                continue
            except Exception as exc:
                # The network or «retry after»: the rest keeps its order for the next tick.
                logger.warning("monitoring_message_delayed error_type=%s", type(exc).__name__)
                break
            try:
                await self._store.mark_sent(message.id)
            except RepositoryError as exc:
                # The owner has it; the next tick may send it again — better than losing it.
                logger.error("monitoring_message_unrecorded error_type=%s", type(exc).__name__)
                break
            sent += 1
        return sent
