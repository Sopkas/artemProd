"""Provider boundary: caller validates INN using the shared S1-01 validator."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from claims_assistant.domain.external import ExternalSnapshot, Section


@dataclass(frozen=True, slots=True)
class RequestLimits:
    timeout_seconds: float = 20
    max_requests: int = 20
    max_pages: int = 20

    def __post_init__(self) -> None:
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Timeout must be finite and positive")
        for limit in (self.max_requests, self.max_pages):
            if type(limit) is not int or limit <= 0:
                raise ValueError("Request and page limits must be positive integers")


@dataclass(frozen=True, slots=True)
class CompanyDataRequest:
    inn: str
    sections: tuple[Section, ...] = tuple(Section)
    limits: RequestLimits = RequestLimits()
    # Asked before every request a section makes beyond its first (a later EFRSB page): the
    # caller's run budget pays for it. Given the other sections of this request as they
    # stand — a snapshot once answered, None while in flight — so units held for their
    # retries are held only while one may still be owed. Returns the timeout that request
    # may take, never past the budget's deadline, or None when it may not be made: the
    # section then stops with what it has, once no sibling is left in flight. Without it
    # only ``limits`` apply (review G5: twenty pages were charged to the run as one request,
    # and each got a fresh timeout past the deadline).
    next_request: Callable[[Sequence[ExternalSnapshot | None]], float | None] | None = None

    def __post_init__(self) -> None:
        # This is a type guard, not a second INN/checksum validator.
        if not isinstance(self.inn, str) or not self.inn:
            raise ValueError("INN must be a non-empty string validated by the caller")
        if not isinstance(self.sections, tuple) or not self.sections:
            raise ValueError("Sections must be a non-empty tuple")
        if any(not isinstance(section, Section) for section in self.sections):
            raise ValueError("Unsupported section")
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("Duplicate section")


class CompanyDataProvider(Protocol):
    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        """Return one snapshot per requested section, in request order.

        Expected source failures are section results, not exceptions. Programming
        errors and cancellation propagate. Adapters enforce the supplied limits;
        the caller controls the overall run budget. A live failure never switches
        to a demo provider.
        """
        ...
