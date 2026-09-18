"""Sprint-2 processing step: re-read the stored package and record how usable it is.

This is the placeholder body of the pipeline. S3-01 replaces it with import → provider
→ scoring → report; the worker, queue and outcomes stay the same.
"""

import asyncio

from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.imports import IssueSeverity

from .analysis_queue import RunOutcome
from .check_package import FileStorage, StorageError
from .imports import SheetReader, import_counterparties

NO_MAIN_FILE = "В пакете нет файла «Контрагенты»."
FILE_UNREADABLE = "Файл пакета недоступен или повреждён; загрузите проверку заново."
NO_USABLE_ROWS = "В файле не осталось пригодных строк; исправьте данные и загрузите заново."


class PackageProcessor:
    def __init__(self, files: FileStorage, reader: SheetReader) -> None:
        self._files = files
        self._reader = reader

    async def process(self, run: AnalysisRun) -> RunOutcome:
        main = [file for file in run.files if file.kind == FileKind.COUNTERPARTIES]
        if not main:
            return RunOutcome(RunStatus.FAILED, NO_MAIN_FILE)
        try:
            data = await asyncio.to_thread(self._files.read, main[0].stored_path)
        except StorageError:
            return RunOutcome(RunStatus.FAILED, FILE_UNREADABLE)
        result = await asyncio.to_thread(
            import_counterparties, self._reader, data, analysis_date=run.analysis_date
        )
        if not result.rows:
            return RunOutcome(RunStatus.FAILED, NO_USABLE_ROWS)
        errors = sum(1 for issue in result.issues if issue.severity is IssueSeverity.ERROR)
        if errors:
            return RunOutcome(
                RunStatus.PARTIAL,
                f"Строк с ошибками: {errors}; они исключены из проверки.",
            )
        return RunOutcome(RunStatus.COMPLETED)
