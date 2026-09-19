"""Sprint-2 processing step: re-read the stored package and record how usable it is.

This is the placeholder body of the pipeline. S3-01 replaces it with import → provider
→ scoring → report; the worker, queue and outcomes stay the same. Since S3-03 the import
step is stored by key (run, RUN_SCOPE, "import", version): after an interruption the
resumed run reuses the saved result instead of parsing the file again.
"""

import asyncio
import json
from datetime import UTC, datetime

from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.imports import IssueSeverity
from claims_assistant.domain.steps import RUN_SCOPE, StepResult, StepStatus

from .analysis_queue import RunOutcome
from .check_package import FileStorage, StorageError
from .imports import SheetReader, import_counterparties
from .step_store import StepStore

NO_MAIN_FILE = "В пакете нет файла «Контрагенты»."
FILE_UNREADABLE = "Файл пакета недоступен или повреждён; загрузите проверку заново."
NO_USABLE_ROWS = "В файле не осталось пригодных строк; исправьте данные и загрузите заново."

IMPORT_STEP = "import"
IMPORT_VERSION = "counterparties-v1"


class PackageProcessor:
    def __init__(
        self, files: FileStorage, reader: SheetReader, steps: StepStore | None = None
    ) -> None:
        self._files = files
        self._reader = reader
        self._steps = steps

    async def process(self, run: AnalysisRun) -> RunOutcome:
        saved = await self._saved_import(run)
        if saved is not None:
            return saved
        outcome, payload = await self._import(run)
        await self._save_import(run, outcome, payload)
        return outcome

    async def _import(self, run: AnalysisRun) -> tuple[RunOutcome, dict | None]:
        main = [file for file in run.files if file.kind == FileKind.COUNTERPARTIES]
        if not main:
            return RunOutcome(RunStatus.FAILED, NO_MAIN_FILE), None
        try:
            data = await asyncio.to_thread(self._files.read, main[0].stored_path)
        except StorageError:
            return RunOutcome(RunStatus.FAILED, FILE_UNREADABLE), None
        result = await asyncio.to_thread(
            import_counterparties, self._reader, data, analysis_date=run.analysis_date
        )
        if not result.rows:
            return RunOutcome(RunStatus.FAILED, NO_USABLE_ROWS), None
        errors = sum(1 for issue in result.issues if issue.severity is IssueSeverity.ERROR)
        payload = {"rows": len(result.rows), "errors": errors}
        if errors:
            return (
                RunOutcome(
                    RunStatus.PARTIAL, f"Строк с ошибками: {errors}; они исключены из проверки."
                ),
                payload,
            )
        return RunOutcome(RunStatus.COMPLETED), payload

    async def _saved_import(self, run: AnalysisRun) -> RunOutcome | None:
        if self._steps is None:
            return None
        step = await self._steps.get_step(run.id, RUN_SCOPE, IMPORT_STEP, IMPORT_VERSION)
        if step is None:
            return None
        if step.status is StepStatus.FAILED:
            return RunOutcome(RunStatus.FAILED, step.error)
        counts = json.loads(step.payload or "{}")
        errors = int(counts.get("errors", 0))
        if errors:
            return RunOutcome(
                RunStatus.PARTIAL, f"Строк с ошибками: {errors}; они исключены из проверки."
            )
        return RunOutcome(RunStatus.COMPLETED)

    async def _save_import(
        self, run: AnalysisRun, outcome: RunOutcome, payload: dict | None
    ) -> None:
        if self._steps is None:
            return
        if payload is None:
            result = StepResult(
                run_id=run.id,
                inn=RUN_SCOPE,
                step=IMPORT_STEP,
                version=IMPORT_VERSION,
                status=StepStatus.FAILED,
                completed_at=datetime.now(UTC),
                error=outcome.failure,
            )
        else:
            result = StepResult(
                run_id=run.id,
                inn=RUN_SCOPE,
                step=IMPORT_STEP,
                version=IMPORT_VERSION,
                status=StepStatus.OK,
                completed_at=datetime.now(UTC),
                payload=json.dumps(payload, ensure_ascii=False),
            )
        await self._steps.save_step(result)
