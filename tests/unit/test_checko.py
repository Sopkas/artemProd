import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock

import aiohttp
import pytest

from claims_assistant.application.company_data import CompanyDataRequest, RequestLimits
from claims_assistant.domain.external import CompanyStatus, Coverage, DataMode, FetchStatus, Section
from claims_assistant.infrastructure.checko import company_data as checko

INN = "1234567894"
NOW = datetime(2026, 9, 18, tzinfo=UTC)
KEY = "synthetic-private-key"
# Synthetic values; field names/types verified against a live company response.
PAYLOAD = {
    "meta": {"status": "ok"},
    "data": {
        "ИНН": INN,
        "ОГРН": "0000000000000",
        "НаимСокр": "ДЕМО ТЕСТ",
        "ДатаВып": "2026-09-01",
        "Статус": {"Код": "001", "Наим": "Действует"},
    },
}


class Transport:
    def __init__(self, status=200, payload=PAYLOAD, error=None):
        self.status, self.payload, self.error = status, payload, error
        self.calls = []

    async def request(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.status, self.payload


def provider(transport):
    return checko.CheckoCompanyDataProvider(KEY, transport, lambda: NOW)


async def test_live_company_projection_and_unimplemented_sections():
    transport = Transport()
    snapshots = await provider(transport).fetch(CompanyDataRequest(INN))
    assert len(transport.calls) == 1
    assert transport.calls[0] == (KEY, INN, 20)
    assert [s.section for s in snapshots] == list(Section)
    assert all(s.mode == DataMode.LIVE for s in snapshots)
    company = snapshots[0]
    assert company.status == FetchStatus.OK
    assert company.coverage == Coverage.PARTIAL
    assert company.facts[1].value == CompanyStatus.ACTIVE
    assert company.facts[0].observed_on.isoformat() == "2026-09-01"
    assert company.fetched_at == NOW
    assert company.source_updated_at is None
    assert company.evidence[0].record_id == "0000000000000"
    for other in snapshots[1:]:
        assert other.coverage == Coverage.UNAVAILABLE
        assert other.error.code == "not_implemented"
        assert not other.facts
    assert KEY not in repr(snapshots)
    assert KEY not in repr(provider(transport))


async def test_no_network_for_unsupported_sections_and_preserves_order():
    transport = Transport()
    sections = (Section.FINANCES, Section.BANKRUPTCY)
    snapshots = await provider(transport).fetch(CompanyDataRequest(INN, sections))
    assert tuple(s.section for s in snapshots) == sections
    assert transport.calls == []


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        {"Код": "000", "Наим": "Не действует"},
        {"Код": "001", "Наим": "Иной статус"},
        {"Код": "999", "Наим": "Действует"},
    ],
)
async def test_unknown_status_is_never_active_or_liquidated(status):
    payload = deepcopy(PAYLOAD)
    payload["data"]["Статус"] = status
    snapshot = (await provider(Transport(payload=payload)).fetch(CompanyDataRequest(INN)))[0]
    assert snapshot.status == FetchStatus.OK
    assert snapshot.coverage == Coverage.PARTIAL
    assert snapshot.facts[1].value is None
    assert snapshot.facts[1].missing_reason


@pytest.mark.parametrize(
    "field,value",
    [
        ("ИНН", "0000000000"),
        ("ИНН", 1234567894),
        ("ОГРН", None),
        ("ДатаВып", None),
        ("ДатаВып", "yesterday"),
        ("ДатаВып", "2026-02-30"),
        ("ДатаВып", "2099-01-01"),
        ("НаимСокр", []),
        ("НаимСокр", "x" * 1001),
    ],
)
async def test_malformed_company_becomes_invalid_response(field, value):
    payload = deepcopy(PAYLOAD)
    payload["data"][field] = value
    snapshot = (await provider(Transport(payload=payload)).fetch(CompanyDataRequest(INN)))[0]
    assert snapshot.status == FetchStatus.INVALID_RESPONSE
    assert not snapshot.facts


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"meta": []},
        {"meta": {"status": "unknown"}},
        {"meta": {"status": "ok"}, "data": {}},
    ],
)
async def test_unknown_envelope_is_not_no_risk(payload):
    snapshot = (await provider(Transport(payload=payload)).fetch(CompanyDataRequest(INN)))[0]
    assert snapshot.status == FetchStatus.INVALID_RESPONSE


@pytest.mark.parametrize(
    "http,status,code",
    [
        (401, FetchStatus.UNAUTHORIZED, "access_denied"),
        (403, FetchStatus.UNAUTHORIZED, "access_denied"),
        (429, FetchStatus.RATE_LIMITED, "rate_limited"),
        (500, FetchStatus.UNAVAILABLE, "http_error"),
        (302, FetchStatus.UNAVAILABLE, "http_error"),
        (404, FetchStatus.UNAVAILABLE, "http_error"),
    ],
)
async def test_http_error_is_safe_and_not_retried(http, status, code):
    transport = Transport(http, {"private": KEY})
    snapshots = await provider(transport).fetch(CompanyDataRequest(INN))
    assert snapshots[0].status == status
    assert snapshots[0].error.code == code
    assert len(transport.calls) == 1
    assert KEY not in repr(snapshots)


async def test_api_error_body_is_not_echoed(caplog):
    payload = {"meta": {"status": "error", "message": KEY}}
    snapshots = await provider(Transport(payload=payload)).fetch(CompanyDataRequest(INN))
    assert snapshots[0].error.code == "api_error"
    assert KEY not in repr(snapshots) + caplog.text


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError(KEY), "timeout"),
        (aiohttp.ClientConnectionError(KEY), "network_error"),
        (OSError(KEY), "network_error"),
        (checko.InvalidResponse(), "invalid_response"),
    ],
)
async def test_expected_transport_failures_are_safe(error, code, caplog):
    snapshots = await provider(Transport(error=error)).fetch(CompanyDataRequest(INN))
    assert snapshots[0].error.code == code
    assert KEY not in repr(snapshots) + caplog.text


@pytest.mark.parametrize("error", [asyncio.CancelledError(), RuntimeError("bug")])
async def test_cancellation_and_bugs_propagate(error):
    with pytest.raises(type(error)):
        await provider(Transport(error=error)).fetch(CompanyDataRequest(INN))


async def test_deadline_is_enforced_even_for_custom_transport():
    class Slow:
        async def request(self, *args):
            await asyncio.Event().wait()

    snapshots = await provider(Slow()).fetch(
        CompanyDataRequest(
            INN, limits=RequestLimits(timeout_seconds=0.01, max_requests=1, max_pages=1)
        )
    )
    assert snapshots[0].error.code == "timeout"


@pytest.mark.parametrize(
    "body,error",
    [
        (b"not json", True),
        (b"x" * (checko.MAX_RESPONSE_BYTES + 1), True),
        (json.dumps(PAYLOAD).encode(), False),
    ],
)
async def test_http_transport_keeps_key_out_of_url_and_closes(monkeypatch, body, error):
    closed = []

    class Response:
        status = 200

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            for offset in range(0, len(body), size):
                yield body[offset : offset + size]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("response")

    class Session:
        def post(self, url, **kwargs):
            assert url == checko.COMPANY_URL
            assert KEY not in url
            assert kwargs == {"json": {"key": KEY, "inn": INN}, "allow_redirects": False}
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("session")

    constructor = Mock(return_value=Session())
    monkeypatch.setattr(checko.aiohttp, "ClientSession", constructor)
    if error:
        with pytest.raises(checko.InvalidResponse):
            await checko.AiohttpCompanyTransport().request(KEY, INN, 2)
    else:
        assert await checko.AiohttpCompanyTransport().request(KEY, INN, 2) == (200, PAYLOAD)
    assert constructor.call_args.kwargs["trust_env"] is False
    assert constructor.call_args.kwargs["timeout"].total == 2
    assert closed == ["response", "session"]


async def test_live_card_through_telegram_without_real_api(bot, update_factory):
    from claims_assistant.presentation.telegram.handlers import create_dispatcher

    dispatcher = create_dispatcher(frozenset({42}), provider(Transport()))
    await dispatcher.feed_update(bot, update_factory("/inn"))
    await dispatcher.feed_update(bot, update_factory(INN))
    text = bot.session.calls[-1].text
    assert "ИНН " + INN in text
    assert "синтетические данные" not in text
    assert "checko-company-v2" in text
    assert "Раздел ещё не подключён" in text
    assert KEY not in text


async def test_source_error_is_visible_in_live_card_without_fallback(bot, update_factory):
    from claims_assistant.presentation.telegram.handlers import create_dispatcher

    dispatcher = create_dispatcher(frozenset({42}), provider(Transport(status=429)))
    await dispatcher.feed_update(bot, update_factory("/inn"))
    await dispatcher.feed_update(bot, update_factory(INN))
    text = bot.session.calls[-1].text
    assert "Достигнут лимит запросов Checko" in text
    assert "ДЕМО" not in text
    assert "сообщений не найдено" not in text


async def test_full_name_is_used_when_short_name_is_missing():
    payload = deepcopy(PAYLOAD)
    payload["data"]["НаимСокр"] = None
    payload["data"]["НаимПолн"] = "Синтетическое полное название"
    snapshot = (await provider(Transport(payload=payload)).fetch(CompanyDataRequest(INN)))[0]
    assert snapshot.facts[0].value == payload["data"]["НаимПолн"]


async def test_extract_dated_today_in_moscow_is_not_rejected_near_utc_midnight():
    # 21:30 UTC is 00:30 the next day in Moscow; an extract dated that Moscow day is
    # current, not future, and must be accepted rather than treated as invalid.
    late_utc = datetime(2026, 9, 18, 21, 30, tzinfo=UTC)
    payload = deepcopy(PAYLOAD)
    payload["data"]["ДатаВып"] = "2026-09-19"
    live = checko.CheckoCompanyDataProvider(KEY, Transport(payload=payload), lambda: late_utc)
    snapshot = (await live.fetch(CompanyDataRequest(INN)))[0]
    assert snapshot.status == FetchStatus.OK
    assert snapshot.facts[0].observed_on.isoformat() == "2026-09-19"


async def test_extract_after_the_moscow_day_is_still_rejected():
    # A date beyond the current Moscow day is implausible and stays invalid_response.
    late_utc = datetime(2026, 9, 18, 21, 30, tzinfo=UTC)
    payload = deepcopy(PAYLOAD)
    payload["data"]["ДатаВып"] = "2026-09-20"
    live = checko.CheckoCompanyDataProvider(KEY, Transport(payload=payload), lambda: late_utc)
    snapshot = (await live.fetch(CompanyDataRequest(INN)))[0]
    assert snapshot.status == FetchStatus.INVALID_RESPONSE
