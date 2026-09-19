"""Step results and report artifacts (S3-03 contract), implemented by the run storage.

Steps are system-level: the worker writes and reads them for every owner. The report is
read by its owner (Telegram) and delivery is recorded apart from the analysis result, so
resending a finished report never queues a new check.
"""

from typing import Protocol

from claims_assistant.domain.steps import DeliveryStatus, ReportArtifact, StepResult


class StepStore(Protocol):
    async def save_step(self, result: StepResult) -> StepResult:
        """Store a result; an existing result with the same key wins and is returned.

        RunNotFound if the run does not exist.
        """
        ...

    async def get_step(
        self, run_id: str, inn: str, step: str, version: str
    ) -> StepResult | None: ...

    async def list_steps(self, run_id: str) -> tuple[StepResult, ...]:
        """All saved results of a run in save order; empty for an unknown run."""
        ...


class ReportStore(Protocol):
    async def save_report(self, run_id: str, stored_path: str) -> ReportArtifact:
        """Record a freshly built report as pending delivery, replacing an older one."""
        ...

    async def get_report(self, owner_id: int, run_id: str) -> ReportArtifact | None:
        """The run's report, or None when none was built; RunNotFound for other owners."""
        ...

    async def mark_delivery(
        self, run_id: str, status: DeliveryStatus, error: str | None = None
    ) -> ReportArtifact:
        """Record the outcome of sending the report; RunNotFound without a report."""
        ...
