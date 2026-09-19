"""Retention of checks (S6-02): expired runs are removed with their files.

A run expires when it is in a final state (or is a draft nobody launched) and has not
changed for the retention period. Expired runs are deleted together with their uploaded
files, report file and step results — the debtor data belongs to the customer and must not
outlive the period agreed with them. Queued and running checks are never touched.

The sweep is idempotent and safe to repeat: a file that is already gone is not an error,
and a storage failure on one run does not stop the others.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from claims_assistant.domain.analysis import AnalysisRun, RunStatus

from .check_package import FileStorage, StorageError

logger = logging.getLogger(__name__)

EXPIRABLE_STATUSES = frozenset(
    {RunStatus.DRAFT, RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED}
)


class MaintenanceStore(Protocol):
    async def list_expired(self, before: datetime) -> tuple[AnalysisRun, ...]:
        """Runs in an expirable state whose last change is before the moment, oldest first."""
        ...

    async def is_expired(self, run_id: str, before: datetime) -> bool:
        """Whether the run is still in an expirable state and unchanged since ``before``."""
        ...

    async def delete_run(self, run_id: str, before: datetime) -> bool:
        """Remove the run with its files, steps and report record — only if it is still
        expired as of ``before`` (the same condition as ``list_expired``). False when the
        run changed meanwhile or does not exist."""
        ...


class RunFileStorage(FileStorage, Protocol):
    def remove_run(self, run_id: str) -> None:
        """Remove the run's directory with everything in it; a missing directory is fine."""
        ...


@dataclass(frozen=True, slots=True)
class PurgeSummary:
    expired: int  # runs found past the retention period
    removed: int  # runs whose records were deleted
    failed: int  # runs left in place because their files could not be removed
    dry_run: bool = False


async def purge_expired(
    repository: MaintenanceStore,
    files: RunFileStorage,
    *,
    retention: timedelta,
    now: datetime,
    dry_run: bool = False,
) -> PurgeSummary:
    """Delete every run older than the retention period; files first, records second."""
    if retention <= timedelta(0):
        raise ValueError("Retention must be a positive period")
    before = now - retention
    expired = await repository.list_expired(before)
    removed = failed = 0
    for run in expired:
        if dry_run:
            continue
        # A draft launched (or a failed run relaunched) between the listing and now is
        # live again: leave it alone, files included.
        if not await repository.is_expired(run.id, before):
            continue
        try:
            await asyncio.to_thread(files.remove_run, run.id)
        except StorageError:
            # Records stay so the next sweep retries; nothing is orphaned silently.
            logger.error("purge_files_failed run_id=%s", run.id)
            failed += 1
            continue
        if await repository.delete_run(run.id, before):
            removed += 1
            logger.info("run_purged run_id=%s status=%s", run.id, run.status)
    return PurgeSummary(expired=len(expired), removed=removed, failed=failed, dry_run=dry_run)


class RetentionSweeper:
    """Runs the purge at start and then every interval, next to the worker (S6-02)."""

    def __init__(
        self,
        repository: MaintenanceStore,
        files: RunFileStorage,
        *,
        retention: timedelta,
        interval: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._files = files
        self._retention = retention
        self._interval = interval
        self._clock = clock or _utc_now
        self._stop = asyncio.Event()

    async def sweep_once(self) -> PurgeSummary:
        summary = await purge_expired(
            self._repository, self._files, retention=self._retention, now=self._clock()
        )
        logger.info(
            "retention_sweep expired=%s removed=%s failed=%s",
            summary.expired,
            summary.removed,
            summary.failed,
        )
        return summary

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Storage trouble: log the type and try again next interval.
                logger.error("retention_sweep_failed error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def _utc_now() -> datetime:
    return datetime.now(UTC)
