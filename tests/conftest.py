import faulthandler
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

# Safety net covering collection and fixtures, which pytest-timeout does not: if the
# session stalls (seen on Windows CI), dump every thread's stack and exit rather than
# letting the job burn its full time budget with no diagnostic.
faulthandler.dump_traceback_later(float(os.environ.get("PYTEST_FAULT_TIMEOUT", "120")), exit=True)

TEST_TOKEN = "123456789:synthetic_token_for_offline_tests_only"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self.closed = False
        self.fail_once: Exception | None = None

    async def close(self) -> None:
        self.closed = True

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if self.fail_once is not None:
            error, self.fail_once = self.fail_once, None
            raise error
        if isinstance(method, SendMessage):
            return Message(
                message_id=100,
                date=datetime.now(UTC),
                chat={"id": method.chat_id, "type": "private"},
                text=method.text,
            )
        raise AssertionError(f"Unexpected Telegram request: {type(method).__name__}")

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise AssertionError("Files must never be downloaded")
        yield b""  # pragma: no cover


@pytest.fixture
async def bot() -> AsyncGenerator[Bot, None]:
    instance = Bot(token=TEST_TOKEN, session=RecordingSession())
    yield instance
    await instance.session.close()


@pytest.fixture
def update_factory():
    def make(
        text: str | None = "/start",
        user_id: int = 42,
        chat_type: str = "private",
        **message_fields: Any,
    ) -> Update:
        message: dict[str, Any] = {
            "message_id": 1,
            "date": datetime.now(UTC),
            "chat": {"id": user_id if chat_type == "private" else -123, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            **message_fields,
        }
        if text is not None:
            message["text"] = text
        return Update(update_id=1, message=message)

    return make
