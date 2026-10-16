"""The monitoring list in SQLite (S8-09): entries, last good snapshots, days and outbox.

Every method is owner-scoped where an owner exists, and blocking work runs in a worker
thread. A check is recorded in one transaction — its snapshots, the entry's last result
and its notice in the outbox — so a restart between them cannot send a notice twice or
lose one. A snapshot or a result the code can no longer read is dropped as if it were
not there: the company gets a new baseline instead of stopping the day.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.monitoring import CheckRecord, DayResult, Outgoing
from claims_assistant.domain.external import FactKind, Section
from claims_assistant.domain.monitoring import Change, Comparison, SectionState
from claims_assistant.domain.serialization import (
    PayloadError,
    snapshot_from_dict,
    snapshot_to_dict,
)
from claims_assistant.domain.watchlist import (
    WATCH_LIMIT,
    AddResult,
    Candidate,
    RemoveResult,
    WatchEntry,
    add,
    remove,
)

from .schema import monitoring_days, monitoring_outbox, watch_snapshots, watchlist

logger = logging.getLogger(__name__)

CHANGE = "change"
SUMMARY = "summary"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _day(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def comparison_to_json(comparison: Comparison) -> str:
    changes = [
        {
            "section": change.section.value,
            "kind": change.kind.value,
            "text": change.text,
            "observed_on": change.observed_on.isoformat() if change.observed_on else None,
            "record_id": change.record_id,
            "url": change.url,
            "notify": change.notify,
        }
        for change in comparison.changes
    ]
    data = {
        "inn": comparison.inn,
        "changes": changes,
        "states": [[section.value, state.value] for section, state in comparison.states],
        "notes": [[section.value, note] for section, note in comparison.notes],
    }
    return json.dumps(data, ensure_ascii=False)


def comparison_from_json(raw: str) -> Comparison:
    data = json.loads(raw)
    inn = data["inn"]
    changes = tuple(
        Change(
            inn,
            Section(item["section"]),
            FactKind(item["kind"]),
            item["text"],
            _day(item["observed_on"]),
            item["record_id"],
            item["url"],
            bool(item["notify"]),
        )
        for item in data["changes"]
    )
    states = tuple((Section(s), SectionState(state)) for s, state in data["states"])
    notes = tuple((Section(s), str(note)) for s, note in data["notes"])
    return Comparison(inn, changes, states, notes)


def _entry(row: Any) -> WatchEntry:
    return WatchEntry(
        inn=row.inn,
        added_on=date.fromisoformat(row.added_on),
        name=row.name,
        last_checked_on=_day(row.last_checked_on),
        last_change=row.last_change,
        last_change_on=_day(row.last_change_on),
    )


class SqliteWatchStore:
    """``WatchStore`` over the service's own database."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime] = _now) -> None:
        self._engine = engine
        self._clock = clock

    async def _run(self, operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except SQLAlchemyError as error:
            raise RepositoryError("Сбой хранилища мониторинга.") from error

    # --- the list ---

    async def entries(self, owner_id: int) -> tuple[WatchEntry, ...]:
        return await self._run(self._entries, owner_id)

    async def owners(self) -> tuple[int, ...]:
        return await self._run(self._owners)

    async def add(
        self,
        owner_id: int,
        candidates: Sequence[Candidate],
        added_on: date,
        limit: int = WATCH_LIMIT,
    ) -> AddResult:
        return await self._run(self._add, owner_id, tuple(candidates), added_on, limit)

    async def remove(self, owner_id: int, inns: Sequence[str]) -> RemoveResult:
        return await self._run(self._remove, owner_id, tuple(inns))

    async def clear(self, owner_id: int) -> int:
        return await self._run(self._clear, owner_id)

    def _entries(self, owner_id: int) -> tuple[WatchEntry, ...]:
        query = (
            select(watchlist)
            .where(watchlist.c.owner_id == owner_id)
            .order_by(watchlist.c.sequence.desc())
        )
        with self._engine.connect() as connection:
            return tuple(_entry(row) for row in connection.execute(query))

    def _owners(self) -> tuple[int, ...]:
        query = select(watchlist.c.owner_id).distinct().order_by(watchlist.c.owner_id)
        with self._engine.connect() as connection:
            return tuple(connection.execute(query).scalars())

    def _inns(self, connection: Connection, owner_id: int) -> list[str]:
        query = select(watchlist.c.inn).where(watchlist.c.owner_id == owner_id)
        return list(connection.execute(query).scalars())

    def _add(
        self, owner_id: int, candidates: tuple[Candidate, ...], added_on: date, limit: int
    ) -> AddResult:
        with self._engine.connect() as connection:
            # The write lock before the read: two lists sent at once must not both fit
            # into the last free places.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                result = add(self._inns(connection, owner_id), candidates, limit)
                last = connection.execute(
                    select(func.coalesce(func.max(watchlist.c.sequence), 0)).where(
                        watchlist.c.owner_id == owner_id
                    )
                ).scalar_one()
                for offset, candidate in enumerate(result.added, start=1):
                    connection.execute(
                        insert(watchlist).values(
                            owner_id=owner_id,
                            inn=candidate.inn,
                            name=candidate.name,
                            added_on=added_on.isoformat(),
                            sequence=last + offset,
                        )
                    )
                for candidate in result.already:
                    if candidate.name:  # a name from a file fills a gap, never overwrites
                        connection.execute(
                            update(watchlist)
                            .where(
                                watchlist.c.owner_id == owner_id,
                                watchlist.c.inn == candidate.inn,
                                watchlist.c.name.is_(None),
                            )
                            .values(name=candidate.name)
                        )
            except Exception:
                connection.exec_driver_sql("ROLLBACK")
                raise
            connection.exec_driver_sql("COMMIT")
        return result

    def _forget(self, connection: Connection, owner_id: int, inns: Sequence[str] | None) -> None:
        """The entries and their snapshots: watching a company again starts afresh."""
        for table in (watch_snapshots, watchlist):
            condition = table.c.owner_id == owner_id
            if inns is not None:
                condition = and_(condition, table.c.inn.in_(inns))
            connection.execute(delete(table).where(condition))

    def _remove(self, owner_id: int, inns: tuple[str, ...]) -> RemoveResult:
        with self._engine.begin() as connection:
            result = remove(self._inns(connection, owner_id), inns)
            if result.removed:
                self._forget(connection, owner_id, result.removed)
        return result

    def _clear(self, owner_id: int) -> int:
        with self._engine.begin() as connection:
            count = len(self._inns(connection, owner_id))
            self._forget(connection, owner_id, None)
        return count

    # --- checks ---

    async def snapshots(self, owner_id: int, inn: str):
        return await self._run(self._snapshots, owner_id, inn)

    async def record_check(self, owner_id: int, record: CheckRecord) -> bool:
        return await self._run(self._record_check, owner_id, record)

    async def results(self, owner_id: int, day: date) -> tuple[DayResult, ...]:
        return await self._run(self._results, owner_id, day)

    def _snapshots(self, owner_id: int, inn: str):
        query = select(watch_snapshots.c.section, watch_snapshots.c.payload).where(
            watch_snapshots.c.owner_id == owner_id, watch_snapshots.c.inn == inn
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        snapshots = []
        for row in rows:
            try:
                snapshot = snapshot_from_dict(json.loads(row.payload))
            except (PayloadError, ValueError, TypeError, KeyError):
                logger.warning("watch_snapshot_unreadable section=%s", row.section)
                continue
            if snapshot.inn == inn:
                snapshots.append(snapshot)
        return tuple(snapshots)

    def _record_check(self, owner_id: int, record: CheckRecord) -> bool:
        key = (watchlist.c.owner_id == owner_id, watchlist.c.inn == record.inn)
        values: dict[str, Any] = {
            "last_checked_on": record.day.isoformat(),
            "last_result": comparison_to_json(record.comparison),
            "name": record.name,
        }
        notices = record.comparison.to_notify
        if notices:
            values["last_change"] = notices[0].text[:500]
            values["last_change_on"] = record.day.isoformat()
        now = _stamp(self._clock())
        with self._engine.begin() as connection:
            if connection.execute(update(watchlist).where(*key).values(**values)).rowcount == 0:
                return False  # taken off the list meanwhile: nothing to keep for it
            for snapshot in record.snapshots:
                connection.execute(
                    delete(watch_snapshots).where(
                        watch_snapshots.c.owner_id == owner_id,
                        watch_snapshots.c.inn == record.inn,
                        watch_snapshots.c.section == snapshot.section.value,
                    )
                )
                connection.execute(
                    insert(watch_snapshots).values(
                        owner_id=owner_id,
                        inn=record.inn,
                        section=snapshot.section.value,
                        payload=json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False),
                        fetched_at=_stamp(snapshot.fetched_at),
                    )
                )
            if record.message is not None:
                connection.execute(
                    insert(monitoring_outbox).values(
                        owner_id=owner_id,
                        day=record.day.isoformat(),
                        kind=CHANGE,
                        inn=record.inn,
                        text=record.message,
                        created_at=now,
                    )
                )
        return True

    def _results(self, owner_id: int, day: date) -> tuple[DayResult, ...]:
        query = (
            select(watchlist.c.inn, watchlist.c.name, watchlist.c.last_result)
            .where(
                watchlist.c.owner_id == owner_id,
                watchlist.c.last_checked_on == day.isoformat(),
                watchlist.c.last_result.is_not(None),
            )
            .order_by(watchlist.c.sequence)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        results = []
        for row in rows:
            try:
                comparison = comparison_from_json(row.last_result)
            except (ValueError, TypeError, KeyError):
                logger.warning("watch_result_unreadable")
                continue
            results.append(DayResult(row.inn, row.name, comparison))
        return tuple(results)

    # --- days ---

    async def day_finished(self, day: date) -> bool:
        return await self._run(self._day_finished, day)

    async def start_day(self, day: date) -> None:
        await self._run(self._start_day, day)

    async def finish_day(self, day: date) -> None:
        await self._run(self._finish_day, day)

    def _day_finished(self, day: date) -> bool:
        query = select(monitoring_days.c.finished_at).where(
            monitoring_days.c.day == day.isoformat()
        )
        with self._engine.connect() as connection:
            return connection.execute(query).scalar_one_or_none() is not None

    def _start_day(self, day: date) -> None:
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(monitoring_days.c.day).where(monitoring_days.c.day == day.isoformat())
            ).first()
            if exists is None:
                connection.execute(
                    insert(monitoring_days).values(
                        day=day.isoformat(), started_at=_stamp(self._clock())
                    )
                )

    def _finish_day(self, day: date) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(monitoring_days)
                .where(monitoring_days.c.day == day.isoformat())
                .values(finished_at=_stamp(self._clock()))
            )

    # --- outbox ---

    async def queue_summary(self, owner_id: int, day: date, text: str) -> bool:
        return await self._run(self._queue_summary, owner_id, day, text)

    async def has_summary(self, owner_id: int, day: date) -> bool:
        return await self._run(self._has_summary, owner_id, day)

    async def pending(self, limit: int) -> tuple[Outgoing, ...]:
        return await self._run(self._pending, limit)

    async def mark_sent(self, message_id: int) -> None:
        await self._run(self._mark, message_id, {"sent_at": _stamp(self._clock())})

    async def mark_failed(self, message_id: int, reason: str) -> None:
        await self._run(self._mark, message_id, {"failed": reason[:200]})

    async def purge_messages(self, before: datetime) -> int:
        return await self._run(self._purge, before)

    def _summary_exists(self, connection: Connection, owner_id: int, day: date) -> bool:
        query = select(monitoring_outbox.c.id).where(
            monitoring_outbox.c.owner_id == owner_id,
            monitoring_outbox.c.day == day.isoformat(),
            monitoring_outbox.c.kind == SUMMARY,
        )
        return connection.execute(query).first() is not None

    def _has_summary(self, owner_id: int, day: date) -> bool:
        with self._engine.connect() as connection:
            return self._summary_exists(connection, owner_id, day)

    def _queue_summary(self, owner_id: int, day: date, text: str) -> bool:
        with self._engine.begin() as connection:
            if self._summary_exists(connection, owner_id, day):
                return False
            connection.execute(
                insert(monitoring_outbox).values(
                    owner_id=owner_id,
                    day=day.isoformat(),
                    kind=SUMMARY,
                    text=text,
                    created_at=_stamp(self._clock()),
                )
            )
        return True

    def _pending(self, limit: int) -> tuple[Outgoing, ...]:
        query = (
            select(monitoring_outbox)
            .where(monitoring_outbox.c.sent_at.is_(None), monitoring_outbox.c.failed.is_(None))
            .order_by(monitoring_outbox.c.id)
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        return tuple(
            Outgoing(row.id, row.owner_id, row.text, row.inn if row.kind == CHANGE else None)
            for row in rows
        )

    def _mark(self, message_id: int, values: dict[str, str]) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(monitoring_outbox)
                .where(monitoring_outbox.c.id == message_id)
                .values(**values)
            )

    def _purge(self, before: datetime) -> int:
        """Delivered and refused messages older than the checks are kept; waiting ones stay."""
        done = monitoring_outbox.c.sent_at.is_not(None) | monitoring_outbox.c.failed.is_not(None)
        with self._engine.begin() as connection:
            result = connection.execute(
                delete(monitoring_outbox).where(
                    done, monitoring_outbox.c.created_at < _stamp(before)
                )
            )
        return result.rowcount
