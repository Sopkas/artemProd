"""Process-local TTL cache of successful section snapshots, keyed by (inn, section).

The cached snapshot keeps its original fetched_at, so a card or report built from the
cache shows when the data was really obtained (docs/architecture.md: cache dates are
shown, one hour is a setting, not a promise of registry freshness).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from claims_assistant.domain.external import ExternalSnapshot, FetchStatus, Section


def _now() -> datetime:
    return datetime.now(UTC)


class TtlSnapshotCache:
    def __init__(self, ttl_seconds: float, clock: Callable[[], datetime] = _now) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._items: dict[tuple[str, Section], tuple[datetime, ExternalSnapshot]] = {}

    def get(self, inn: str, section: Section) -> ExternalSnapshot | None:
        item = self._items.get((inn, section))
        if item is None:
            return None
        stored_at, snapshot = item
        if self._clock() - stored_at > self._ttl:
            del self._items[(inn, section)]
            return None
        return snapshot

    def put(self, snapshot: ExternalSnapshot) -> None:
        if snapshot.status is not FetchStatus.OK:
            return
        self._items[(snapshot.inn, snapshot.section)] = (self._clock(), snapshot)

    def clear(self) -> None:
        self._items.clear()
