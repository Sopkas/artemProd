"""Resend a finished report without a new check (S3-03).

The report is the newest one the owner has: a draft or a queued check started later does
not hide it. It is read from file storage and handed to the presentation layer; delivery
is recorded apart from the run status. Nothing here touches the queue, so asking for the
report never triggers processing.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from claims_assistant.domain.analysis import AnalysisRun
from claims_assistant.domain.steps import DeliveryStatus, ReportArtifact

from .analysis_repository import AnalysisRepository
from .check_package import FileStorage, StorageError
from .step_store import ReportStore

REPORT_FILE_MISSING = "Файл отчёта отсутствует в хранилище."


class ReportRepository(AnalysisRepository, ReportStore, Protocol):
    """Run listing and report records of one storage."""


class ReportUnavailable(RuntimeError):
    """The report exists in storage records but its file cannot be read."""


@dataclass(frozen=True, slots=True)
class ReportFile:
    run: AnalysisRun
    artifact: ReportArtifact
    filename: str
    data: bytes


async def latest_report(
    owner_id: int, repository: ReportRepository
) -> tuple[AnalysisRun, ReportArtifact] | None:
    """The newest run of the owner that has a report, newest first."""
    for run in await repository.list_runs(owner_id):
        artifact = await repository.get_report(owner_id, run.id)
        if artifact is not None:
            return run, artifact
    return None


async def fetch_report(
    owner_id: int, repository: ReportRepository, files: FileStorage
) -> ReportFile | None:
    """The owner's newest report, or None when no check has produced one yet.

    A recorded report whose file is gone marks the delivery failed and raises
    ReportUnavailable; the run itself is left untouched.
    """
    found = await latest_report(owner_id, repository)
    if found is None:
        return None
    run, artifact = found
    try:
        data = await asyncio.to_thread(files.read, artifact.stored_path)
    except StorageError:
        await repository.mark_delivery(run.id, DeliveryStatus.FAILED, error=REPORT_FILE_MISSING)
        raise ReportUnavailable() from None
    filename = f"otchet-{run.analysis_date.isoformat()}.xlsx"
    return ReportFile(run=run, artifact=artifact, filename=filename, data=data)


async def confirm_delivery(
    report: ReportFile, repository: ReportStore, *, error: str | None = None
) -> ReportArtifact:
    status = DeliveryStatus.FAILED if error else DeliveryStatus.DELIVERED
    return await repository.mark_delivery(report.run.id, status, error=error)
