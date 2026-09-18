"""S1-05: one bounded HTTPS request for organization details only."""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
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

SOURCE = "checko-company-v2"
COMPANY_URL = "https://api.checko.ru/v2/company"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class InvalidResponse(ValueError):
    """Malformed or excessive response, without its contents."""


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
    if observed_on > fetched_at.date():
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
    ) -> None:
        if not api_key.strip():
            raise ValueError("Checko API key is required")
        self._api_key = api_key.strip()
        self._transport = transport if transport is not None else AiohttpCompanyTransport()
        self._clock = clock

    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        # At most one request, no pagination and no automatic retries; all valid
        # RequestLimits therefore permit this adapter's request/page budget.
        fetched_at = self._clock()
        company = None
        if Section.COMPANY in request.sections:
            company = await self._company(request, fetched_at)
        return tuple(
            company
            if section == Section.COMPANY
            else _failure(
                request.inn,
                section,
                fetched_at,
                FetchStatus.UNAVAILABLE,
                "not_implemented",
                "Раздел ещё не подключён; сведения не проверены.",
            )
            for section in request.sections
        )

    async def _company(self, request: CompanyDataRequest, fetched_at: datetime) -> ExternalSnapshot:
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
            if http_status == 200:
                return normalize_company(payload, request.inn, fetched_at)
            if http_status in (401, 403):
                status, code, message = (
                    FetchStatus.UNAUTHORIZED,
                    "access_denied",
                    "Checko не разрешил доступ.",
                )
            elif http_status == 429:
                status, code, message = (
                    FetchStatus.RATE_LIMITED,
                    "rate_limited",
                    "Достигнут лимит запросов Checko.",
                )
            else:
                code, message = "http_error", "Checko вернул ошибку HTTP."
        except TimeoutError:
            code, message = "timeout", "Checko не ответил за отведённое время."
        except (aiohttp.ClientError, OSError):
            pass
        except InvalidResponse:
            status, code, message = (
                FetchStatus.INVALID_RESPONSE,
                "invalid_response",
                "Ответ Checko не соответствует ожидаемому формату.",
            )
        return _failure(request.inn, Section.COMPANY, fetched_at, status, code, message)
