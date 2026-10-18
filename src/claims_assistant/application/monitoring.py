"""S8-09 (part A): the monitoring service — the list, the daily run and what it leaves.

What counts as a change is part B's (``domain.monitoring.compare``, S8-06); how a message
reads is too (``presentation/telegram/monitoring``). Here is the rest:

- **The list is its owner's**, as checks are: one Telegram user's companies, up to the
  agreed limit (S8-08). Taking a company off forgets its snapshots, so watching it again
  starts from a new baseline.
- **One run a business day**, at the agreed time or later: a bot that was off at that
  hour runs when it starts. A run is resumable — a company checked today is not asked
  again after a restart, so a crash costs no second day's requests.
- **The last successful snapshot of each section is kept**, not yesterday's: a case that
  appeared on a day the source was down is still new the next day (S8-06).
- **Every message goes to an outbox first**, in the same transaction as the check it
  reports; the notifier sends it when Telegram takes it. An afternoon without Telegram
  (11.10 and 13.10.2026) must not lose a bankruptcy notice.
- **A demo snapshot is never compared with a live one**: switching the source is a new
  baseline, not a hundred «new» cases.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Protocol

from claims_assistant.domain.external import DataMode, ExternalSnapshot, FactKind, FetchStatus
from claims_assistant.domain.monitoring import MONITORED, Comparison, SectionState, compare
from claims_assistant.domain.watchlist import (
    WATCH_LIMIT,
    AddResult,
    Candidate,
    RemoveResult,
    WatchEntry,
)

from .company_data import CompanyDataProvider, CompanyDataRequest
from .external_guard import BUDGET_EXHAUSTED, DAILY_LIMIT, SOURCE_LIMIT, RunBudget

logger = logging.getLogger(__name__)

# The day's own note when the bot itself failed on a company: never «изменений нет».
INTERNAL_FAILURE = "Не удалось проверить: внутренняя ошибка бота."
# A section the source was never asked about (the day's budget, the source closed for the
# day) or refused for the day. A company with nothing but these waits for the next run
# instead of being recorded as checked, and the next run starts with it (17.10.2026: on
# the free tariff the same newest companies were checked every day and the rest never).
_LIMIT_CODES = frozenset({BUDGET_EXHAUSTED, SOURCE_LIMIT, DAILY_LIMIT})
_WAITS = object()


WEEK = 7


def _now() -> datetime:
    return datetime.now(UTC)


class Frequency(StrEnum):
    """How often an owner's list is checked (S8-12, the customer 17.10.2026: «чтобы не
    тратить деньги»)."""

    DAILY = "daily"
    WEEKDAYS = "weekdays"
    # Each company once a week, a seventh of the list every day: about the same requests
    # every day, within the free hundred for a list of a hundred.
    WEEKLY = "weekly"


@dataclass(frozen=True, slots=True)
class WatchSettings:
    frequency: Frequency = Frequency.DAILY
    # The pause began this business day; no requests until it is lifted. The list and its
    # last snapshots stay, so the comparison goes on from where it stopped.
    paused_since: date | None = None
    # The business day the schedule last changed: a resume or another frequency. The own
    # weekdays before it are not made up — nobody wanted those checks then.
    changed_on: date | None = None

    @property
    def paused(self) -> bool:
        return self.paused_since is not None


def weekly_slot(inn: str) -> int:
    """The weekday (0 = Monday) a company is checked on in the weekly mode. From the INN, so
    it does not move when other companies leave the list."""
    return int(inn) % WEEK


def is_due(entry: WatchEntry, day: date, settings: WatchSettings, limited: bool) -> bool:
    """Whether a company is checked on ``day`` in its owner's mode.

    Weekly: on its own weekday, and the next day when the requests ran out on it. A company
    whose own weekday came and went without its check — the computer off, the day's
    requests spent before it — is made up on the next run. One that was not due then (on a
    pause, in another frequency, not on the list yet) waits for its weekday: so neither a
    long list sent at once, nor a resume, nor a switch from every day checks the whole list
    in one day, and every company keeps its weekday. («A week has passed» did both: on a
    resume after a pause it checked the whole list, and a switch from every day moved a
    seventh of it off its day for good — Andrey's review of #111, 17.10.2026.)
    """
    if settings.frequency is Frequency.DAILY:
        return True
    if settings.frequency is Frequency.WEEKDAYS:
        return day.weekday() < 5
    if limited:
        return True
    back = (day.weekday() - weekly_slot(entry.inn)) % WEEK
    if back == 0:
        return True
    own_day = day - timedelta(days=back)  # the last one before today
    if settings.changed_on is not None and own_day < settings.changed_on:
        return False
    if entry.last_checked_on is None:
        return entry.added_on <= own_day
    return entry.last_checked_on < own_day


@dataclass(frozen=True, slots=True)
class MonitoringPolicy:
    # 09:00 by the business clock until the customer names another hour (S8-07).
    run_at: time = time(9, 0)
    utc_offset_hours: int = 3
    # Whether a quiet day still gets its summary; the customer's call, open (S8-07).
    quiet_summary: bool = True
    # One day's run: 100 companies × 5 sections, with room for pages and retries.
    max_requests: int = 1000
    max_seconds: float = 3 * 3600
    # Sent messages hold names and cases; they are kept no longer than the checks are.
    keep_messages_days: int = 30
    limit: int = WATCH_LIMIT

    def __post_init__(self) -> None:
        if self.max_requests <= 0 or self.max_seconds <= 0 or self.limit <= 0:
            raise ValueError("Monitoring limits must be positive")


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """What one company's check leaves: its good snapshots, the comparison and a message."""

    inn: str
    day: date
    name: str | None
    snapshots: tuple[ExternalSnapshot, ...]
    comparison: Comparison
    message: str | None  # the change notice, None when there is nothing worth writing
    # The day's requests ran out on some of its sections: it goes first next time, so the
    # sections it missed do not wait a whole round (review B on #105).
    limited: bool = False


@dataclass(frozen=True, slots=True)
class DayResult:
    inn: str
    name: str | None
    comparison: Comparison


@dataclass(frozen=True, slots=True)
class Outgoing:
    id: int
    owner_id: int
    text: str
    # The company of a change notice: its message gets the «Больше не следить» button.
    inn: str | None = None


@dataclass(frozen=True, slots=True)
class DayOutcome:
    day: date
    owners: int = 0
    checked: int = 0
    changed: int = 0
    unchecked: int = 0
    failed: int = 0
    waiting: int = 0  # not reached: the requests ran out, they go first next time


class WatchStore(Protocol):
    async def entries(self, owner_id: int) -> tuple[WatchEntry, ...]: ...

    async def owners(self) -> tuple[int, ...]: ...

    async def settings(self, owner_id: int) -> WatchSettings: ...

    async def save_settings(self, owner_id: int, settings: WatchSettings) -> None: ...

    async def add(
        self, owner_id: int, candidates: Sequence[Candidate], added_on: date, limit: int
    ) -> AddResult: ...

    async def remove(self, owner_id: int, inns: Sequence[str]) -> RemoveResult: ...

    async def clear(self, owner_id: int) -> int: ...

    async def snapshots(self, owner_id: int, inn: str) -> tuple[ExternalSnapshot, ...]: ...

    async def record_check(self, owner_id: int, record: CheckRecord) -> bool: ...

    async def results(self, owner_id: int, day: date) -> tuple[DayResult, ...]: ...

    async def limited(self, owner_id: int) -> frozenset[str]:
        """INNs whose last check ran out of requests on some section."""
        ...

    async def queue_summary(self, owner_id: int, day: date, text: str) -> bool: ...

    async def has_summary(self, owner_id: int, day: date) -> bool: ...

    async def day_finished(self, day: date) -> bool: ...

    async def start_day(self, day: date) -> None: ...

    async def finish_day(self, day: date) -> None: ...

    async def pending(self, limit: int) -> tuple[Outgoing, ...]: ...

    async def mark_sent(self, message_id: int) -> None: ...

    async def mark_failed(self, message_id: int, reason: str) -> None: ...

    async def purge_messages(self, before: datetime) -> int: ...


class MonitoringTexts(Protocol):
    """The words of part B; the service only decides when they are written."""

    def changes(
        self, inn: str, name: str | None, comparison: Comparison, day: date
    ) -> str | None: ...

    def summary(self, day: date, results: Sequence[DayResult], waiting: int = 0) -> str:
        """``waiting``: companies the day's requests did not reach."""
        ...


class ScopedProvider(Protocol):
    def scoped(self, budget: RunBudget) -> CompanyDataProvider: ...


def company_name(snapshots: Sequence[ExternalSnapshot]) -> str | None:
    for snapshot in snapshots:
        if snapshot.status is not FetchStatus.OK:
            continue
        for fact in snapshot.facts:
            if fact.kind is FactKind.COMPANY_NAME and isinstance(fact.value, str):
                return fact.value.strip()[:300] or None
    return None


def waits_for_limit(snapshots: Sequence[ExternalSnapshot]) -> bool:
    """Every section is the limit's placeholder: nothing about the company was learned."""
    return bool(snapshots) and all(
        snapshot.status is not FetchStatus.OK
        and snapshot.error is not None
        and snapshot.error.code in _LIMIT_CODES
        for snapshot in snapshots
    )


def queue_order(
    entries: Sequence[WatchEntry], limited: frozenset[str] = frozenset()
) -> list[WatchEntry]:
    """Never checked and cut short by the limit last time first, then the longest
    unchecked; among equals, the store's order (newest first)."""

    def key(entry: WatchEntry) -> tuple[bool, date]:
        settled = entry.last_checked_on is not None and entry.inn not in limited
        return settled, entry.last_checked_on or date.min

    return sorted(entries, key=key)


def failed_comparison(inn: str) -> Comparison:
    """Every section unchecked, with the bot's own reason: a company the bot failed on is
    named in the summary instead of passing for a quiet one."""
    return Comparison(
        inn,
        (),
        tuple((section, SectionState.UNCHECKED) for section in MONITORED),
        tuple((section, INTERNAL_FAILURE) for section in MONITORED),
    )


class MonitoringService:
    def __init__(
        self,
        store: WatchStore,
        provider: ScopedProvider,
        texts: MonitoringTexts,
        *,
        mode: DataMode,
        policy: MonitoringPolicy = MonitoringPolicy(),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._store = store
        self._provider = provider
        self._texts = texts
        self._mode = mode
        self.policy = policy
        self._clock = clock
        self._zone = timezone(timedelta(hours=policy.utc_offset_hours))

    # --- the list, from the dialog ---

    def today(self) -> date:
        return self._clock().astimezone(self._zone).date()

    def local_now(self) -> datetime:
        """The business clock: the hour of the run is set by it."""
        return self._clock().astimezone(self._zone)

    async def entries(self, owner_id: int) -> tuple[WatchEntry, ...]:
        return await self._store.entries(owner_id)

    async def add(self, owner_id: int, candidates: Sequence[Candidate]) -> AddResult:
        return await self._store.add(owner_id, candidates, self.today(), self.policy.limit)

    async def remove(self, owner_id: int, inns: Sequence[str]) -> RemoveResult:
        return await self._store.remove(owner_id, inns)

    async def clear(self, owner_id: int) -> int:
        return await self._store.clear(owner_id)

    # --- pause and frequency (S8-12) ---

    async def settings(self, owner_id: int) -> WatchSettings:
        return await self._store.settings(owner_id)

    async def pause(self, owner_id: int) -> WatchSettings:
        current = await self._store.settings(owner_id)
        if current.paused:
            return current
        paused = replace(current, paused_since=self.today())
        await self._store.save_settings(owner_id, paused)
        return paused

    async def resume(self, owner_id: int) -> WatchSettings:
        current = await self._store.settings(owner_id)
        if not current.paused:
            return current
        resumed = replace(current, paused_since=None, changed_on=self.today())
        await self._store.save_settings(owner_id, resumed)
        return resumed

    async def set_frequency(self, owner_id: int, frequency: Frequency) -> WatchSettings:
        current = await self._store.settings(owner_id)
        if current.frequency is frequency:
            return current
        changed = replace(current, frequency=frequency, changed_on=self.today())
        await self._store.save_settings(owner_id, changed)
        return changed

    async def next_run(self, owner_id: int) -> date | None:
        """The business day the owner's list is next looked at; None on a pause. In the
        weekly mode a part of the list is checked every day, so it is tomorrow or today."""
        settings = await self._store.settings(owner_id)
        if settings.paused:
            return None
        moment = self._clock().astimezone(self._zone)
        day = moment.date()
        if await self._store.day_finished(day):
            day += timedelta(days=1)
        while settings.frequency is Frequency.WEEKDAYS and day.weekday() >= 5:
            day += timedelta(days=1)
        return day

    # --- the daily run ---

    async def due(self) -> date | None:
        """Today's business date when its run is due: the hour has come and the run has
        not finished (never started, or cut short by a restart)."""
        moment = self._clock().astimezone(self._zone)
        if moment.time() < self.policy.run_at:
            return None
        day = moment.date()
        return None if await self._store.day_finished(day) else day

    async def run_day(self, day: date) -> DayOutcome:
        await self._store.start_day(day)
        budget = RunBudget.for_run(self.policy.max_requests, self.policy.max_seconds, self._clock())
        source = self._provider.scoped(budget)
        owners = await self._store.owners()
        checked = changed = unchecked = failed = waiting = 0
        for owner_id in owners:
            waits = 0
            settings = await self._store.settings(owner_id)
            if settings.paused:
                continue  # not a request on a pause, and no summary to say so
            entries = await self._store.entries(owner_id)
            limited = await self._store.limited(owner_id)
            for entry in queue_order(entries, limited):
                if entry.last_checked_on == day:
                    continue  # done before a restart
                if not is_due(entry, day, settings, entry.inn in limited):
                    continue
                done = await self._check(owner_id, entry, day, source)
                if done is None:
                    continue  # taken off the list while it was being checked
                if done is _WAITS:
                    waits += 1
                    continue
                comparison, broke = done
                checked += 1
                changed += bool(comparison.to_notify)
                unchecked += bool(comparison.unchecked)
                failed += broke
            waiting += waits
            await self._summary(owner_id, day, waits)
        await self._store.finish_day(day)
        keep = timedelta(days=self.policy.keep_messages_days)
        await self._store.purge_messages(self._clock() - keep)
        outcome = DayOutcome(day, len(owners), checked, changed, unchecked, failed, waiting)
        logger.info(
            "monitoring_day day=%s owners=%s checked=%s changed=%s unchecked=%s failed=%s "
            "waiting=%s requests=%s",
            day.isoformat(),
            outcome.owners,
            outcome.checked,
            outcome.changed,
            outcome.unchecked,
            outcome.failed,
            outcome.waiting,
            budget.used,
        )
        return outcome

    async def _check(
        self, owner_id: int, entry: WatchEntry, day: date, source: CompanyDataProvider
    ) -> tuple[Comparison, bool] | object | None:
        broke = False
        try:
            current = await source.fetch(CompanyDataRequest(inn=entry.inn, sections=MONITORED))
            if waits_for_limit(current):
                return _WAITS
            limited = any(
                snapshot.error is not None and snapshot.error.code in _LIMIT_CODES
                for snapshot in current
            )
            previous = [
                snapshot
                for snapshot in await self._store.snapshots(owner_id, entry.inn)
                if snapshot.mode is self._mode
            ]
            comparison = compare(entry.inn, previous, current)
            # The customer's own name (his message, file or 1C report) stays; the source's
            # fills a gap (17.10.2026).
            name = entry.name or company_name(current)
            message = self._texts.changes(entry.inn, name, comparison, day)
            good = tuple(s for s in current if s.status is FetchStatus.OK)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One company must not stop the day, nor be retried every minute: it is
            # recorded as checked, with the reason, and the next day tries it again.
            logger.error("monitoring_check_failed error_type=%s", type(exc).__name__)
            comparison, name, message, good = failed_comparison(entry.inn), entry.name, None, ()
            broke, limited = True, False
        record = CheckRecord(entry.inn, day, name, good, comparison, message, limited)
        if not await self._store.record_check(owner_id, record):
            return None
        return comparison, broke

    async def _summary(self, owner_id: int, day: date, waiting: int = 0) -> None:
        if await self._store.has_summary(owner_id, day):
            return
        results = await self._store.results(owner_id, day)
        if not results and not waiting:
            return
        quiet = not waiting and all(result.comparison.nothing_new for result in results)
        if quiet and not self.policy.quiet_summary:
            return
        text = self._texts.summary(day, results, waiting)
        await self._store.queue_summary(owner_id, day, text)


class MonitoringScheduler:
    """Next to the run worker: delivers what waits, runs the day when it is due.

    A failed run is tried again after a pause, not on the next tick: the store or the
    code failing every minute would ask the source every minute.
    """

    def __init__(
        self,
        service: MonitoringService,
        deliver: Callable[[], Awaitable[int]],
        *,
        interval: float = 60.0,
        retry_after: float = 900.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._service = service
        self._deliver = deliver
        self._interval = interval
        self._retry_after = timedelta(seconds=retry_after)
        self._clock = clock
        self._failed_at: datetime | None = None
        self._stop = asyncio.Event()

    async def tick(self) -> DayOutcome | None:
        await self._safe_deliver()
        outcome = None
        if self._failed_at is None or self._clock() - self._failed_at >= self._retry_after:
            try:
                day = await self._service.due()
                if day is not None:
                    outcome = await self._service.run_day(day)
                self._failed_at = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed_at = self._clock()
                logger.error("monitoring_run_failed error_type=%s", type(exc).__name__)
        if outcome is not None:
            await self._safe_deliver()
        return outcome

    async def _safe_deliver(self) -> None:
        try:
            await self._deliver()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("monitoring_delivery_failed error_type=%s", type(exc).__name__)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
