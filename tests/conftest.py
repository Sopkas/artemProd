import socket
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendDocument, SendMessage
from aiogram.types import File, Message, Update

# The repository root, so `evals/` (the model bench set, S5-07) imports like a package.
# It is not part of the installed service: only the tests and the bench script read it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_make_parametrize_id(config, val, argname):
    # Keep large parametrize values (e.g. a >2 MiB response body) out of the test ID.
    # pytest stores that ID in PYTEST_CURRENT_TEST, and Windows caps an environment
    # variable at 32767 chars, so a huge repr aborts the whole session with ValueError.
    text = repr(val)
    return None if len(text) <= 60 else f"{argname}<{len(text)} chars>"


_LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}


def _host(value: object) -> object:
    return value.decode() if isinstance(value, bytes) else value


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Tests are offline: a DNS lookup or connection beyond loopback fails the test.

    Loopback stays allowed because asyncio builds its internal socket pair through it on
    Windows. Attempts are also recorded, so a leak fails even if an adapter swallowed it.
    """
    attempts: list[object] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host, *args, **kwargs):
        if _host(host) not in _LOCAL_HOSTS:
            attempts.append(_host(host))
            raise RuntimeError(f"Network access in tests: {_host(host)!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and _host(address[0]) not in _LOCAL_HOSTS:
            attempts.append(_host(address[0]))
            raise RuntimeError(f"Network access in tests: {_host(address[0])!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
    assert not attempts, f"Tests must not use the network: {attempts}"


TEST_TOKEN = "123456789:synthetic_token_for_offline_tests_only"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self.closed = False
        self.fail_once: Exception | None = None
        # Bytes served for a known file_id; anything else must never be downloaded.
        self.files: dict[str, bytes] = {}

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
        if isinstance(method, SendDocument):
            return Message(
                message_id=101,
                date=datetime.now(UTC),
                chat={"id": method.chat_id, "type": "private"},
                document={"file_id": "sent", "file_unique_id": "sent"},
            )
        if isinstance(method, GetFile) and method.file_id in self.files:
            return File(file_id=method.file_id, file_unique_id="u", file_path=method.file_id)
        raise AssertionError(f"Unexpected Telegram request: {type(method).__name__}")

    async def stream_content(
        self, url: str, *args: Any, **kwargs: Any
    ) -> AsyncGenerator[bytes, None]:
        for file_id, data in self.files.items():
            if url.endswith("/" + file_id):
                yield data
                return
        raise AssertionError("Files must never be downloaded")


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
