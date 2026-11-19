"""One process owns durable Studio development polling and its guide at a time."""
import asyncio
import logging

from chatcopilot.core.private_sqlite import private_lock
from studio.setup import Guide


class StudioWorker:
    def __init__(self, studio):
        self.studio = studio
        self.guide = Guide(studio.state_dir, studio.framework, model_path=studio.model_path)

    async def run(self):
        self.guide.worker_managed = True
        # A second entry remains usable, but never starts another guide or consumer.
        while True:
            try:
                with private_lock(self.studio.state_dir / "development-worker.lock"):
                    await self._owned()
            except BlockingIOError:
                await asyncio.sleep(1)
            except Exception:
                logging.getLogger(__name__).warning("Studio development storage unavailable")
                await asyncio.sleep(1)

    async def _owned(self):
        try:
            while True:
                pending = await self._finish_thread(self.studio.pending)
                for message in pending:
                    with self.studio.db.connect() as db:
                        row = db.execute("SELECT state FROM messages WHERE id=?", (message,)).fetchone()
                    if row and row["state"] in {"thinking", "planning"}:
                        try:
                            await self._finish_thread(self.guide.start)
                        except Exception:
                            # Discussion setup cannot block an existing build.
                            logging.getLogger(__name__).warning("Studio guide startup unavailable")
                            if row["state"] == "planning":
                                self.studio.update(message, "plan_failed", "请在平台管理中连接讨论模型，再重新生成方案。")
                            continue
                    try:
                        await self._finish_thread(self.studio.step, message)
                    except Exception:
                        logging.getLogger(__name__).warning("Studio development polling failed")
                await asyncio.sleep(1)
        finally:
            await self._finish_thread(self.guide.stop)

    @staticmethod
    async def _finish_thread(function, *args):
        # Cancelling to_thread does not stop its thread. Retain ownership until
        # that step really finishes, so shutdown cannot overlap a new consumer.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
