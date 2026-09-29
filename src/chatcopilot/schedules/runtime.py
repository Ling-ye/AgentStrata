"""Small bot-host loop; Console restarts do not own or interrupt execution."""
from __future__ import annotations

import asyncio
import logging

from chatcopilot.schedules.models import ScheduleExecutor, ScheduleSettings, occurrence
from chatcopilot.schedules.service import ScheduleService

_LOG = logging.getLogger(__name__)


class ScheduleRuntime:
    def __init__(self, service: ScheduleService, executor: ScheduleExecutor, *, ready, interval: float = 2):
        self.service, self.executor, self.ready, self.interval = service, executor, ready, interval
        self._loop: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._run_id: str | None = None

    async def start(self) -> None:
        # Called only while this bot owns the existing exclusive Gateway instance lease.
        for run in self.service.unfinished():
            if run["status"] == "queued":
                settings = ScheduleSettings.model_validate(run["settings"])
                if run["trigger"] == "timer" and occurrence(settings, self.service.clock(), forward=False) > run["scheduled_for"]:
                    self.service.finish(run["id"], {"status": "skipped", "error_code": "superseded_schedule"})
                continue
            self.service.finish(run["id"], self.executor.recover(run))
        self.service.heartbeat("running")
        self._loop = asyncio.create_task(self._poll(), name="bot-schedules")

    async def tick(self) -> None:
        self.service.heartbeat("running" if self.ready() else "waiting_for_gateway")
        self.service.enqueue_due()
        if self._worker is not None:
            if self._worker.done():
                self._settle_worker()
            elif not self._worker.cancelling() and self.service.detail(self._run_id)["cancel_requested"]:
                self._worker.cancel()
        if self._worker is None and self.ready():
            run = self.service.claim()
            if run:
                self._run_id = run["id"]
                self._worker = asyncio.create_task(self._execute(run), name=run["id"])

    def _settle_worker(self) -> None:
        worker = self._worker
        if worker is None or not worker.done():
            return
        if worker.cancelled() or worker.exception() is not None:
            run = self.service.detail(self._run_id)
            outcome = self.executor.recover(run)
            if worker.cancelled() and outcome["status"] == "interrupted":
                outcome = {"status": "cancelled", "error_code": "cancelled"}
            # Keep the completed worker attached until the receipt can be persisted.
            self.service.finish(run["id"], outcome)
        self._worker = None
        self._run_id = None

    async def _poll(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                _LOG.exception("Scheduled task polling failed")
                try:
                    self.service.heartbeat("error", "schedule_storage_error")
                except Exception:
                    _LOG.error("Scheduled task health could not be persisted")
            await asyncio.sleep(self.interval)

    async def _execute(self, run: dict) -> None:
        try:
            async with asyncio.timeout(run["settings"]["timeout_seconds"]):
                outcome = await self.executor.execute(run,
                    on_generated=lambda text: self.service.generated(run["id"], text))
        except (asyncio.CancelledError, TimeoutError) as exc:
            current = self.service.detail(run["id"])
            outcome = self.executor.recover(current)
            if outcome["status"] == "interrupted":
                outcome = {"status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                           "error_code": "cancelled" if isinstance(exc, asyncio.CancelledError) else "timeout"}
        except Exception as exc:
            if (getattr(exc, "code", "") == "session_run_active"
                    and self.service.clock() - run["created_at"] < run["settings"]["timeout_seconds"]):
                self.service.requeue(run["id"])
                return
            outcome = self.executor.recover(self.service.detail(run["id"]))
            if outcome["status"] == "interrupted":
                outcome = {"status": "failed", "error_code": getattr(exc, "code", "execution_failed")}
        self.service.finish(run["id"], outcome)

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
        if self._worker:
            if not self._worker.cancelling():
                self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._settle_worker()
        self.service.heartbeat("stopped")
