"""Monthly AI spend in SQLite (S5-03).

Money is stored as text and added up as ``Decimal``: a binary float would drift, and the
number here is the one compared with the limit the customer agreed on. The addition
happens inside a transaction — two checks finishing at the same moment must not lose one
another's spend, and SQLite's own locking is enough for one service.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError

from claims_assistant.application.analysis_repository import RepositoryError

from .schema import ai_spend


def _amount(raw: object) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        # A row we cannot read must not be taken for «ничего не потрачено»: refusing is
        # the safe answer, since the limit protects the customer's money.
        raise RepositoryError("Счётчик расходов ИИ повреждён.") from None


class SqliteAiSpendStore:
    """``AiSpendStore`` over the service's own database."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    async def spent(self, month: str) -> Decimal:
        return await self._run(self._spent, month)

    async def add(self, month: str, amount: Decimal) -> Decimal:
        if amount <= 0:
            return await self.spent(month)
        return await self._run(self._add, month, amount)

    async def _run(self, operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except SQLAlchemyError as error:
            raise RepositoryError("Сбой хранилища расходов ИИ.") from error

    def _spent(self, month: str) -> Decimal:
        with self._engine.connect() as connection:
            raw = connection.execute(
                select(ai_spend.c.spent_rub).where(ai_spend.c.month == month)
            ).scalar_one_or_none()
        return Decimal(0) if raw is None else _amount(raw)

    def _add(self, month: str, amount: Decimal) -> Decimal:
        stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._engine.begin() as connection:
            raw = connection.execute(
                select(ai_spend.c.spent_rub).where(ai_spend.c.month == month).with_for_update()
                if connection.dialect.name != "sqlite"
                else select(ai_spend.c.spent_rub).where(ai_spend.c.month == month)
            ).scalar_one_or_none()
            total = (Decimal(0) if raw is None else _amount(raw)) + amount
            values = {"spent_rub": format(total, "f"), "updated_at": stamp}
            if raw is None:
                connection.execute(ai_spend.insert().values(month=month, **values))
            else:
                connection.execute(
                    ai_spend.update().where(ai_spend.c.month == month).values(**values)
                )
        return total
