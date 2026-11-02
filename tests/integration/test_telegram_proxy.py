"""Exercise the real Telegram session through a local SOCKS5 stand-in, without internet."""

import asyncio
import json

from aiogram.client.telegram import TelegramAPIServer

from claims_assistant.runtime.app import create_bot
from claims_assistant.runtime.settings import Settings


async def test_telegram_uses_authenticated_proxy_and_remote_dns():
    requests = []

    async def proxy(reader, writer):
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            assert 2 in await reader.readexactly(count)
            writer.write(b"\x05\x02")
            await writer.drain()
            auth_version, length = await reader.readexactly(2)
            assert auth_version == 1
            username = await reader.readexactly(length)
            length = (await reader.readexactly(1))[0]
            password = await reader.readexactly(length)
            assert (username, password) == (b"test-user", b"test-password")
            writer.write(b"\x01\x00")
            await writer.drain()
            # A domain name, not a locally resolved address, must reach the proxy.
            assert await reader.readexactly(4) == b"\x05\x01\x00\x03"
            length = (await reader.readexactly(1))[0]
            assert await reader.readexactly(length) == b"telegram.invalid"
            assert int.from_bytes(await reader.readexactly(2), "big") == 80
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            await writer.drain()
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request.split(b"\r\n", 1)[0])
            body = json.dumps(
                {"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "Test"}}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        settings = Settings(
            "123456789:synthetic_token_for_offline_tests_only",
            frozenset({42}),
            telegram_proxy_url=f"socks5://test-user:test-password@127.0.0.1:{port}",
        )
        bot = create_bot(settings)
        # Plain HTTP is confined to the local stand-in; production keeps Telegram HTTPS.
        bot.session.api = TelegramAPIServer.from_base("http://telegram.invalid")
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=5)
            assert me.id == 123456789
            assert len(requests) == 1
            assert requests[0].endswith(b"/getMe HTTP/1.1")
        finally:
            await bot.session.close()
