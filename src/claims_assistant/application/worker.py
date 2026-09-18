"""The single background worker (S2-02).

It polls the queue, processes one run at a time and records a final outcome. A failing
processor fails that run with a safe reason and the loop continues; the worker itself
never crashes on a run. Blocking work belongs inside the processor (threads), so the
Telegram loop keeps answering while a run is processed.
"""

import asyncio
import logging
from typing import Protocol, runtime_checkable

from claims_assistant.domain.analysis import AnalysisRun, RunStatus

from .analysis_queue import AnalysisQueue, RunOutcome

logger = logging.getLogger(__name__)
PROCESSING_FAILED = "Обработка завершилась ошибкой; обратитесь к разработчику."


@runtime_checkable
class RunProcessor(Protocol):
    async def process(self, run: AnalysisRun) -> RunOutcome:
        """Do the work for one run. Exceptions mean a failed run, not a dead worker."""
        ...


class RunWorker:
    def __init__(
        self, queue: AnalysisQueue, processor: RunProcessor, poll_interval: float = 2.0
    ) -> None:
        self._queue = queue
        self._processor = processor
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def recover(self) -> tuple[AnalysisRun, ...]:
        """Call once at startup, before the loop: requeue runs a dead process left running."""
        recovered = await self._queue.recover_interrupted()
        for run in recovered:
            logger.warning("run_recovered run_id=%s status=%s", run.id, run.status)
        return recovered

    async def process_one(self) -> bool:
        """Claim and process the next queued run; False when the queue is empty."""
        run = await self._queue.claim_next()
        if run is None:
            return False
        logger.info("run_started run_id=%s attempt=%s", run.id, run.attempts)
        try:
            outcome = await self._processor.process(run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The exception text may carry INNs, file contents or provider messages.
            logger.error("run_failed run_id=%s error_type=%s", run.id, type(exc).__name__)
            outcome = RunOutcome(RunStatus.FAILED, PROCESSING_FAILED)
        await self._queue.finish(run.id, outcome)
        logger.info("run_finished run_id=%s status=%s", run.id, outcome.status)
        return True

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                busy = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Storage trouble: log the type and wait, do not spin.
                logger.error("worker_error error_type=%s", type(exc).__name__)
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
