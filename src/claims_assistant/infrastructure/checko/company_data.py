"""S1-05: one bounded HTTPS request for organization details only."""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol

import aiohttp

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    DataMode,
    Evidence,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.inn import InnKind, inn_kind
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.errors import InvalidResponse, NotFound, is_not_found

SOURCE = "checko-company-v2"
COMPANY_URL = "https://api.checko.ru/v2/company"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Checko issues extract dates in Moscow time, which is UTC+3 with no DST since 2014.
_MOSCOW = timezone(timedelta(hours=3))

__all__ = ["CheckoCompanyDataProvider", "AiohttpCompanyTransport", "InvalidResponse", "NotFound"]


class CompanyTransport(Protocol):
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]: ...


class AiohttpCompanyTransport:
    async def request(self, key: str, inn: str, timeout: float) -> tuple[int, object]:
        # One session per check, closed on errors and cancellation as well as success.
        # Keys go in the body; redirects and implicit environment proxies are disabled.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                COMPANY_URL, json={"key": key, "inn": inn}, allow_redirects=False
            ) as response:
                if response.status != 200:
                    return response.status, None
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise InvalidResponse() from None
                try:
                    return response.status, json.loads(body)
                except (ValueError, UnicodeError):
                    raise InvalidResponse() from None


def _now() -> datetime:
    return datetime.now(UTC)


def _http_error(http_status: int) -> tuple[FetchStatus, str, str] | None:
    """Map a non-200 HTTP status to a safe (status, code, message); None means 200."""
    if http_status == 200:
        return None
    if http_status in (401, 403):
        return FetchStatus.UNAUTHORIZED, "access_denied", "Checko не разрешил доступ."
    if http_status == 429:
        return FetchStatus.RATE_LIMITED, "rate_limited", "Достигнут лимит запросов Checko."
    return FetchStatus.UNAVAILABLE, "http_error", "Checko вернул ошибку HTTP."


_API_ERROR = "Checko отклонил запрос; проверьте доступ и лимиты в кабинете."
_INVALID = "Ответ Checko не соответствует ожидаемому формату."
_NOT_FOUND = "Организация с таким ИНН не найдена в источнике."
# S7-02: an entrepreneur is a different record in the source, behind a different method
# we have not been able to check yet. Asking the company method about a 12-digit INN
# would answer «не найдено», which reads as «no such person» — and that is not true.
_ENTREPRENEUR = (
    "Раздел доступен только для организаций; для ИП нужен отдельный метод источника, "
    "он пока не подключён."
)


def _failure(
    inn: str, section: Section, fetched_at: datetime, status: FetchStatus, code: str, message: str
) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn=inn,
        section=section,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=status,
        coverage=Coverage.UNAVAILABLE,
        missing=(message,),
        error=ProviderError(code, message),
    )


def normalize_company(payload: object, inn: str, fetched_at: datetime) -> ExternalSnapshot:
    """Validate the envelope and project only verified fields into the shared contract."""
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise InvalidResponse()
    if payload["meta"].get("status") == "error":
        return _failure(
            inn,
            Section.COMPANY,
            fetched_at,
            FetchStatus.UNAVAILABLE,
            "api_error",
            "Checko отклонил запрос; проверьте доступ и лимиты в кабинете.",
        )
    if payload["meta"].get("status") != "ok":
        raise InvalidResponse()
    data = payload.get("data")
    # An «ok» envelope with empty data is the source's way of saying «no such company».
    if isinstance(data, dict) and not data and is_not_found(payload["meta"]):
        raise NotFound()
    if not isinstance(data, dict) or data.get("ИНН") != inn:
        raise InvalidResponse()
    ogrn = data.get("ОГРН")
    if not isinstance(ogrn, str) or not re.fullmatch(r"[0-9]{13}", ogrn):
        raise InvalidResponse()
    name = data.get("НаимСокр") or data.get("НаимПолн")
    if not isinstance(name, str) or not name.strip() or len(name) > 1000:
        raise InvalidResponse()
    raw_date = data.get("ДатаВып")
    if not isinstance(raw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        raise InvalidResponse()
    try:
        observed_on = date.fromisoformat(raw_date)
    except ValueError:
        raise InvalidResponse() from None
    # ДатаВып is issued in Moscow time (UTC+3, no DST since 2014). Compare against the
    # Moscow calendar date so a fresh extract is not rejected as "future" while UTC is
    # still on the previous day. A fixed offset avoids a tzdata dependency on Windows.
    if observed_on > fetched_at.astimezone(_MOSCOW).date():
        raise InvalidResponse()

    raw_status = data.get("Статус")
    # Only this exact pair has been confirmed on the live API. "Не действует"
    # is not equivalent to liquidation, and unknown codes must not become ACTIVE.
    known_status = (
        isinstance(raw_status, dict)
        and raw_status.get("Код") == "001"
        and raw_status.get("Наим") == "Действует"
    )
    missing = []
    if not known_status:
        missing.append("Статус организации не сопоставлен; проверьте сведения источника.")
    # Keep partial coverage explicit: two fields are not a complete company check.
    missing.append("Получены только название и статус на дату выписки; риски не оценены.")
    evidence = Evidence("company-record", SOURCE, ogrn)
    facts = (
        Fact(
            "company-name",
            inn,
            FactKind.COMPANY_NAME,
            name.strip(),
            (evidence.id,),
            observed_on=observed_on,
        ),
        Fact(
            "company-status",
            inn,
            FactKind.COMPANY_STATUS,
            CompanyStatus.ACTIVE if known_status else None,
            (evidence.id,),
            observed_on=observed_on,
            missing_reason=None if known_status else missing[0],
        ),
    )
    return ExternalSnapshot(
        inn=inn,
        section=Section.COMPANY,
        source=SOURCE,
        mode=DataMode.LIVE,
        fetched_at=fetched_at,
        status=FetchStatus.OK,
        coverage=Coverage.PARTIAL,
        facts=facts,
        evidence=(evidence,),
        missing=tuple(missing),
    )


class CheckoCompanyDataProvider:
    def __init__(
        self,
        api_key: str,
        transport: CompanyTransport | None = None,
        clock: Callable[[], datetime] = _now,
        *,
        bankruptcy_transport: bankruptcy.BankruptcyTransport | None = None,
        finances_transport: finances.FinancesTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Checko API key is required")
        self._api_key = api_key.strip()
        self._transport = transport if transport is not None else AiohttpCompanyTransport()
        self._bankruptcy_transport = bankruptcy_transport or bankruptcy.AiohttpBankruptcyTransport()
        self._finances_transport = finances_transport or finances.AiohttpFinancesTransport()
        self._clock = clock

    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        # One snapshot per requested section, in order; each section fails independently
        # into a section result, never an exception (cancellation/bugs still propagate).
        fetched_at = self._clock()
        handlers = {
            Section.COMPANY: self._company,
            Section.BANKRUPTCY: self._bankruptcy,
            Section.FINANCES: self._finances,
        }
        return tuple([await handlers[section](request, fetched_at) for section in request.sections])

    def _entrepreneur_gap(
        self, request: CompanyDataRequest, section: Section, fetched_at: datetime
    ) -> ExternalSnapshot | None:
        """A section we cannot ask about for an entrepreneur; an honest gap, not a «no»."""
        if inn_kind(request.inn) is InnKind.LEGAL:
            return None
        return _failure(
            request.inn,
            section,
            fetched_at,
            FetchStatus.UNAVAILABLE,
            "not_supported",
            _ENTREPRENEUR,
        )

    async def _company(self, request: CompanyDataRequest, fetched_at: datetime) -> ExternalSnapshot:
        gap = self._entrepreneur_gap(request, Section.COMPANY, fetched_at)
        if gap is not None:
            return gap
        status, code, message = (
            FetchStatus.UNAVAILABLE,
            "network_error",
            "Не удалось связаться с Checko.",
        )
        try:
            async with asyncio.timeout(request.limits.timeout_seconds):
                http_status, payload = await self._transport.request(
                    self._api_key, request.inn, request.limits.timeout_seconds
                )
            mapped = _http_error(http_status)
            if mapped is None:
                return normalize_company(payload, request.inn, fetched_at)
            status, code, message = mapped
        except TimeoutError:
            code, message = "timeout", "Checko не ответил за отведённое время."
        except (aiohttp.ClientError, OSError):
            pass
        except NotFound:
            status, code, message = FetchStatus.NOT_FOUND, "not_found", _NOT_FOUND
        except InvalidResponse:
            status, code, message = FetchStatus.INVALID_RESPONSE, "invalid_response", _INVALID
        return _failure(request.inn, Section.COMPANY, fetched_at, status, code, message)

    async def _bankruptcy(
        self, request: CompanyDataRequest, fetched_at: datetime
    ) -> ExternalSnapshot:
        status, code, message = (
            FetchStatus.UNAVAILABLE,
            "network_error",
            "Не удалось связаться с Checko.",
        )
        try:
            records: list[dict] = []
            limits = request.limits
            budget = min(limits.max_pages, limits.max_requests)
            page = 1
            pages_to_fetch = budget
            total_pages = 1
            while True:
                async with asyncio.timeout(limits.timeout_seconds):
                    http_status, payload = await self._bankruptcy_transport.request(
                        self._api_key, request.inn, page, limits.timeout_seconds
                    )
                mapped = _http_error(http_status)
                if mapped is not None:
                    status, code, message = mapped
                    return _failure(request.inn, Section.BANKRUPTCY, fetched_at, *mapped)
                page_records, total_pages, _current = bankruptcy.read_page(payload, request.inn)
                records.extend(page_records)
                pages_to_fetch = min(total_pages, budget)
                if page >= pages_to_fetch or len(records) >= bankruptcy.MAX_RECORDS:
                    break
                page += 1
            complete = pages_to_fetch >= total_pages and len(records) <= bankruptcy.MAX_RECORDS
            return bankruptcy.project_bankruptcy(
                request.inn, records, fetched_at, complete=complete, unreadable=0
            )
        except TimeoutError:
            code, message = "timeout", "Checko не ответил за отведённое время."
        except (aiohttp.ClientError, OSError):
            pass
        except NotFound:
            status, code, message = FetchStatus.NOT_FOUND, "not_found", _NOT_FOUND
        except bankruptcy.ApiRejected:
            status, code, message = FetchStatus.UNAVAILABLE, "api_error", _API_ERROR
        except bankruptcy.InvalidResponse:
            status, code, message = FetchStatus.INVALID_RESPONSE, "invalid_response", _INVALID
        return _failure(request.inn, Section.BANKRUPTCY, fetched_at, status, code, message)

    async def _finances(
        self, request: CompanyDataRequest, fetched_at: datetime
    ) -> ExternalSnapshot:
        # An entrepreneur files no accounting statements at all: there is nothing to ask for.
        gap = self._entrepreneur_gap(request, Section.FINANCES, fetched_at)
        if gap is not None:
            return gap
        status, code, message = (
            FetchStatus.UNAVAILABLE,
            "network_error",
            "Не удалось связаться с Checko.",
        )
        try:
            async with asyncio.timeout(request.limits.timeout_seconds):
                http_status, payload = await self._finances_transport.request(
                    self._api_key, request.inn, request.limits.timeout_seconds
                )
            mapped = _http_error(http_status)
            if mapped is not None:
                return _failure(request.inn, Section.FINANCES, fetched_at, *mapped)
            years = finances.read_finances(payload)
            return finances.project_finances(request.inn, years, fetched_at)
        except TimeoutError:
            code, message = "timeout", "Checko не ответил за отведённое время."
        except (aiohttp.ClientError, OSError):
            pass
        except NotFound:
            status, code, message = FetchStatus.NOT_FOUND, "not_found", _NOT_FOUND
        except finances.ApiRejected:
            status, code, message = FetchStatus.UNAVAILABLE, "api_error", _API_ERROR
        except finances.InvalidResponse:
            status, code, message = FetchStatus.INVALID_RESPONSE, "invalid_response", _INVALID
        return _failure(request.inn, Section.FINANCES, fetched_at, status, code, message)
